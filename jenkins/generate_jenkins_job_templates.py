#!/usr/bin/env python3
"""从 ci/brands.json 同步 Jenkins 品牌白名单及完整选择器审批哈希。"""

import argparse
import hashlib
import os
import json
import re
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import List, Mapping, Sequence
from xml.sax.saxutils import escape


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from jenkins.sync_jenkins_jobs import JOB_SPECS, JobSpec, project_job_specs
from project_config import ProjectConfigError, load_project, project_template_values, render_project_text

APPROVAL_FILE = REPO_ROOT / 'jenkins/controller/init.groovy.d/03-configure-script-approvals.groovy'
DEFAULT_CONFIG = Path(os.environ.get("APP_SOURCE_ROOT", Path.cwd())) / "ci" / "brands.json"
TEMPLATE_DIR = REPO_ROOT / "jenkins" / "local"
NEUTRAL_TEMPLATE_DIR = REPO_ROOT / "jenkins" / "templates" / "local"
NEUTRAL_SELECTOR_DIR = REPO_ROOT / "jenkins" / "templates" / "selectors"
TAG_FORMATS = {
    "android": ("-v", "该品牌暂无 Android 正式 Tag:selected"),
    "ios": ("-ios-v", "该品牌暂无 iOS 正式 Tag:selected"),
    "harmony": ("-ohos-v", "该品牌暂无鸿蒙正式 Tag:selected"),
}

# 只替换 BRAND 参数的第一个 choices 节点，
# 避免误改 BRANCH_TYPE 等人工维护的选项。
BRAND_CHOICES = re.compile(
    r"(?P<prefix><name>BRAND</name>.*?<choices[^>]*>\s*<a[^>]*>\s*\n)"
    r"(?P<items>(?:\s*<string>[^<]*</string>\s*\n)+)"
    r"(?P<suffix>\s*</a>\s*</choices>)",
    re.DOTALL,
)
SELECTOR_SCRIPT = re.compile(
    r"(?P<prefix><org.biouno.unochoice.CascadeChoiceParameter(?:\s[^>]*)?>.*?<secureScript>\s*<script>)"
    r".*?(?P<suffix></script>)",
    re.DOTALL,
)


class TemplateGenerationError(RuntimeError):
    """品牌配置或 Jenkins 模板结构不满足生成约束。"""


def synchronize_compatibility_snapshots(profile, check):
    """旧 Controller 读取原路径的生产快照，不能把中性占位符放到该路径。"""
    from jenkins.sync_jenkins_admin_jobs import JOB_SPECS as admin_specs
    paths = ['local/admin/' + spec.template_name for spec in admin_specs]
    paths += ['selectors/branch.groovy.template', 'selectors/tag.groovy.template']
    paths += ['pipelines/Jenkinsfile.' + name for name in ('pr-validation', 'ci-pr-validation', 'signing.maintenance', 'signing.backup')]
    changed = []
    for name in paths:
        source = REPO_ROOT / 'jenkins/templates' / name
        target = REPO_ROOT / 'jenkins' / (Path(name).name if name.startswith('pipelines/') else name)
        role = 'ci_pr' if source.name == 'Jenkinsfile.ci-pr-validation' else None
        rendered = render_project_text(source.read_text(encoding='utf-8'), profile, role)
        if not target.exists() or target.read_text(encoding='utf-8') != rendered:
            changed.append(str(target.relative_to(REPO_ROOT)))
            if not check:
                target.write_text(rendered, encoding='utf-8')
    return changed


def load_platform_brands(config_path: Path, platforms=None) -> Mapping[str, Sequence[str]]:
    try:
        root = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TemplateGenerationError(
            "无法读取品牌配置：{}".format(config_path)
        ) from error
    if not isinstance(root, dict):
        raise TemplateGenerationError("ci/brands.json 根节点必须是对象")

    android: List[str] = []
    ios: List[str] = []
    harmony: List[str] = []
    for key, value in root.items():
        if key.startswith("_"):
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or not isinstance(value, dict):
            raise TemplateGenerationError("无效品牌配置：{}".format(key))
        android.append(key)
        if isinstance(value.get("ios"), dict):
            ios.append(key)
        if isinstance(value.get("harmony"), dict):
            harmony.append(key)
    required = set(platforms) if platforms is not None else {"android", "ios"}
    if "android" in required and not android:
        raise TemplateGenerationError("至少需要登记一个 Android 品牌")
    if "ios" in required and not ios:
        raise TemplateGenerationError("至少需要登记一个 iOS 品牌")
    return {"android": android, "ios": ios, "harmony": harmony}


def render_template(source: str, brands: Sequence[str], spec: JobSpec, profile=None) -> str:
    filename = spec.template_name
    if not brands:
        raise TemplateGenerationError("{} 没有已登记的平台品牌".format(filename))
    choice_match = BRAND_CHOICES.search(source)
    if choice_match is None:
        raise TemplateGenerationError("{} 缺少可生成的 BRAND choices".format(filename))
    item_indent_match = re.match(r"\s*", choice_match.group("items"))
    item_indent = item_indent_match.group(0) if item_indent_match else ""
    items = "".join(
        "{}<string>{}</string>\n".format(item_indent, brand) for brand in brands
    )
    rendered = source[:choice_match.start("items")] + items + source[choice_match.end("items"):]

    selectors = ElementTree.fromstring(source).findall('.//org.biouno.unochoice.CascadeChoiceParameter')
    if len(selectors) != 1 or selectors[0].findtext('name') not in ('GIT_BRANCH', 'GIT_TAG'):
        raise TemplateGenerationError("{} 必须包含唯一的分支或 Tag 选择器".format(filename))
    kind = 'branch' if selectors[0].findtext('name') == 'GIT_BRANCH' else 'tag'
    try:
        # XML 是托管生成结果；完整逻辑只在两份规范文本维护，不信任 XML 中的旧脚本。
        groovy = (NEUTRAL_SELECTOR_DIR / (kind + '.groovy.template')).read_text(encoding='utf-8').rstrip('\n')
    except OSError as error:
        raise TemplateGenerationError("无法读取 {} 选择器规范文本".format(kind)) from error
    groovy = groovy.replace('__ALLOWED_BRANDS__', ", ".join("'{}'".format(brand) for brand in brands))
    groovy = groovy.replace('__DEFAULT_BRAND__', brands[0])
    if kind == 'tag':
        suffix, message = TAG_FORMATS[spec.platform]
        groovy = groovy.replace('__TAG_SUFFIX__', suffix).replace('__EMPTY_TAG_MESSAGE__', message)
    groovy = render_project_text(groovy, profile or load_project())
    if profile:
        platform = profile["platforms"][spec.platform]
        if platform["adapter"] != "sxmqlive" and kind == 'branch':
            start = groovy.index("if (branchType == 'feature')")
            end = groovy.index("\nreturn branches.sort()", start)
            groovy = groovy[:start] + "if (branchType != 'default') return ['请先选择分支类型:selected']\nbranches = branches.findAll { it == '" + profile["repositories"]["application"]["defaultBranch"] + "' }\n" + groovy[end:]
            rendered = re.sub(r'(<name>BRANCH_TYPE</name>.*?<a[^>]*>).*?(</a>)', lambda match: match.group(1) + '\n              <string>default</string>\n            ' + match.group(2), rendered, count=1, flags=re.DOTALL)
        if spec.platform != 'harmony':
            choices = ''.join('\n              <string>' + escape(engine) + '</string>' for engine in platform.get('engines', ['cmp','kuikly']))
            rendered = re.sub(r'(<name>UI_ENGINE</name>.*?<a[^>]*>).*?(</a>)', lambda match: match.group(1) + choices + '\n            ' + match.group(2), rendered, count=1, flags=re.DOTALL)
    rendered, count = SELECTOR_SCRIPT.subn(
        lambda match: match.group('prefix') + escape(groovy) + match.group('suffix'), rendered,
    )
    if count != 1:
        raise TemplateGenerationError(
            "{} 缺少唯一可生成的选择器脚本".format(filename)
        )
    return render_project_text(rendered, profile) if profile else rendered


def synchronize(config_path: Path, check: bool, profile=None, output_dir=None) -> int:
    if profile:
        project_template_values(profile)
    brands_by_platform = ({platform: profile["brands"] for platform in profile["platforms"]} if profile and profile.get("brands")
                          else load_platform_brands(config_path, profile["platforms"] if profile else None))
    isolated = profile is not None and profile["id"] != "sxmqlive"
    destination = Path(output_dir) if output_dir else REPO_ROOT / 'build' / 'project-jobs' / profile["id"] if isolated else TEMPLATE_DIR
    approval_destination = destination / APPROVAL_FILE.name if isolated else APPROVAL_FILE
    if not check:
        destination.mkdir(parents=True, exist_ok=True)
    specs = project_job_specs(profile) if profile else JOB_SPECS
    changed = []
    if profile and profile['id'] == 'sxmqlive' and output_dir is None:
        changed += synchronize_compatibility_snapshots(profile, check)
    rendered_templates = {}
    for spec in specs:
        filename = spec.template_name
        path = (NEUTRAL_TEMPLATE_DIR if profile else TEMPLATE_DIR) / filename
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as error:
            raise TemplateGenerationError(
                "无法读取 Jenkins 模板：{}".format(path)
            ) from error
        rendered = render_template(source, brands_by_platform[spec.platform], spec, profile)
        rendered_templates[filename] = rendered
        target = destination / filename
        existing = target.read_text(encoding='utf-8') if target.exists() else None
        if rendered == existing:
            continue
        changed.append(filename)
        if not check:
            target.write_text(rendered, encoding="utf-8")

    approval = APPROVAL_FILE.read_text(encoding='utf-8')
    hashes = []
    for spec in specs:
        root = ElementTree.fromstring(rendered_templates[spec.template_name].replace('__JOB_NAME__', spec.full_name))
        script = root.findtext('.//org.biouno.unochoice.CascadeChoiceParameter/script/secureScript/script')
        hashes.append('SHA512:' + hashlib.sha512(('groovy:' + script).encode('utf-8')).hexdigest())
    block = '// BEGIN MANAGED SELECTOR HASHES\n[\n' + ''.join("    '{}',\n".format(value) for value in hashes)
    block += '].each { approval.approveScript(it) }\n// END MANAGED SELECTOR HASHES'
    updated, count = re.subn(r'// BEGIN MANAGED SELECTOR HASHES\n.*?// END MANAGED SELECTOR HASHES', block, approval, flags=re.DOTALL)
    if count != 1:
        raise TemplateGenerationError('选择器审批缺少唯一的哈希生成段')
    existing_approval = approval_destination.read_text(encoding='utf-8') if approval_destination.exists() else None
    if updated != existing_approval:
        changed.append(str(APPROVAL_FILE.relative_to(REPO_ROOT)))
        if not check:
            approval_destination.write_text(updated, encoding='utf-8')

    if check and isolated and output_dir is None:
        # 新项目没有仓库内的派生 XML/审批副本；门禁验证固定输入的确定性渲染。
        # 实际部署仍须显式 render 并导入该目录的完整脚本审批，不能动态批准线上脚本。
        print("已验证受信项目模板和精确选择器审批；未检查或修改 Controller 部署结果。")
        return 0

    if check and changed:
        print(
            "Jenkins 模板品牌或审批哈希未同步：{}".format(", ".join(changed)),
            file=sys.stderr,
        )
        return 1
    if changed:
        print("已同步 Jenkins 模板：{}".format(", ".join(changed)))
    else:
        print("Jenkins 模板品牌与审批哈希已是最新。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--project", default="sxmqlive")
    parser.add_argument("--output-dir", type=Path, help="新项目独立输出目录，包含精确选择器审批文件")
    parser.add_argument(
        "--check",
        action="store_true",
        help="检查模板品牌与固定审批哈希，不写入文件",
    )
    args = parser.parse_args()
    try:
        return synchronize(args.config.resolve(), args.check, load_project(args.project), args.output_dir)
    except (TemplateGenerationError, ProjectConfigError) as error:
        print("生成 Jenkins 模板失败：{}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
