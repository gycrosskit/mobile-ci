#!/usr/bin/env python3
"""生成无 Git 历史的中性源码包；不发布仓库，不复制生产 profile 或工作区缓存。"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from project_config import _read_static_json, render_project_text, validate_project
from jenkins.sync_jenkins_jobs import JOB_SPECS
import xml.etree.ElementTree as ET

EXCLUDED = {'AGENTS.md', 'SOURCE-MANIFEST.json', 'SOURCE-ORIGIN.json'}
PRIVATE_DOCUMENTS = {'docs/发布流程.md', 'docs/仓库接线与切换.md', 'docs/模板扩展.md',
                     'docs/第三方许可核查.md', 'docs/镜像许可核查.md'}
PRIVATE_PREFIXES = ('projects/', 'docs/audits/image-license')
PUBLIC_SECTIONS = {'docs/GitHub接入.md': '## 2026-10-13 远端私有同仓验收',
                   'docs/Apple证书获取.md': '## 本次验收证据',
                   'docs/鸿蒙环境重建.md': '## 既有统一 Android/OHOS 节点'}
LEGACY_BINDINGS = {'jenkins/Jenkinsfile.test', 'jenkins/Jenkinsfile.release',
                   'jenkins/Jenkinsfile.ios.test', 'jenkins/Jenkinsfile.ios.release', 'jenkins/Jenkinsfile.ohos.test',
                   'jenkins/controller/CiAutoPromotion.groovy', 'jenkins/sync_wiki_navigation.py',
                   'jenkins/template-bindings.json', 'tests/jenkins/test_ci_repository_contract.py',
                   'tests/signing/test_publish_signing_backup.py'}
RUNBOOKS = {'jenkins/local/README.md', 'jenkins/remote/README.md'}
PUBLIC_ENV_EXAMPLES = {'jenkins/local/.env.example', 'jenkins/remote/.env.example'}
FORBIDDEN = {'.git', '.env', 'build', '__pycache__', 'runtime', 'backups'}
SECRET = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{32,}')
NEUTRAL_RUNBOOK = '''# 通用部署入口

这是公开发行包的中性运维入口，未包含公司主机、账号、隧道与部署记录。
安装、节点与凭据要求见 [通用部署](../../docs/部署.md)，验收见 [运行验收](../../docs/运行验收.md)。
生产实例的实际 compose.json、环境、材料、回执和回滚备份由管理员私有保管。
'''

NEUTRAL_DOCUMENT = '''# 通用工具接入

此处的公司部署、生产运维与私有验收记录不随公开源码发行。
公开使用请从 [项目接入](项目接入.md)、[通用部署](部署.md)、
[运行验收](运行验收.md) 与 [GitHub 迁移](GitHub迁移.md) 进入。
原创许可和上游声明见 [发行边界](开源准备.md)；镜像与 SDK 仍须独立审核。
'''


def export(output, root=ROOT):
    root, output = root.resolve(), output.absolute()
    if output.exists() or output.is_symlink() or output.suffixes[-2:] != ['.tar', '.gz']:
        raise ValueError('输出必须是尚不存在的 .tar.gz；拒绝覆盖或链接')
    if any(parent.is_symlink() for parent in output.parents):
        raise ValueError('输出父目录不得包含符号链接')
    names = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard']).decode().split('\0')
    entries = {}
    source_hashes = {}
    executable = set()
    fixture = root / "tests/fixtures/legacy-project.json"
    public_profile = validate_project(_read_static_json(fixture)) if fixture.is_file() else None
    original_bindings = _read_static_json(root / 'jenkins/template-bindings.json') if public_profile else None
    if (root / 'jenkins/templates').exists() and public_profile is None:
        raise ValueError('公开导出缺少中性渲染 fixture')
    if public_profile and public_profile.get('testOnly') is not True:
        raise ValueError('公开导出只允许 testOnly 合成渲染 fixture')
    for name in sorted(set(names) - {''}):
        relative = Path(name)
        if (relative.is_absolute() or '..' in relative.parts or any(part in FORBIDDEN or part.startswith('.env.') and name not in PUBLIC_ENV_EXAMPLES for part in relative.parts)
                or name in EXCLUDED or name.startswith(PRIVATE_PREFIXES)
                or name.startswith('signing/resources/apple-intermediate-certificates/') and relative.suffix == '.pem'):
            continue
        path = root / relative
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError('源码包拒绝链接或非普通文件：' + name)
        if not path.exists():
            continue
        if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_nlink != 1:
            raise ValueError('源码包拒绝链接或非普通文件：' + name)
        if path.stat().st_size > 4 * 1024 * 1024 or path.suffix in {'.p12', '.p7b', '.mobileprovision', '.jks', '.jar', '.apk', '.ipa', '.hap'}:
            raise ValueError('源码包拒绝材料、二进制或过大文件：' + name)
        if path.stat().st_mode & 0o111:
            executable.add(name)
        source_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        neutral_template = root / "jenkins/templates" / relative.relative_to("jenkins") if relative.parts[0] == "jenkins" else None
        if name.startswith("jenkins/Jenkinsfile."):
            neutral_template = root / "jenkins/templates/pipelines" / relative.name
        if public_profile and neutral_template and neutral_template.is_file():
            role = "ci_pr" if relative.name == "Jenkinsfile.ci-pr-validation" else "app_pr" if relative.name == "Jenkinsfile.pr-validation" else None
            data = render_project_text(neutral_template.read_text(), public_profile, role).encode()
        elif name in RUNBOOKS:
            data = NEUTRAL_RUNBOOK.encode()
        elif name in PRIVATE_DOCUMENTS:
            data = NEUTRAL_DOCUMENT.encode()
        else:
            data = path.read_bytes()
            if name in PUBLIC_SECTIONS:
                data = data.decode('utf-8').split(PUBLIC_SECTIONS[name], 1)[0].encode()
        if name in LEGACY_BINDINGS and original_bindings:
            # 仅替换旧 API/fallback 的固定接线值，实际运行仍必须通过受信 profile 校验。
            for role in ('application', 'automation'):
                original = original_bindings['repositories'][role]
                neutral = public_profile['repositories'][role]
                for key in ('ssh', 'https', 'identity'):
                    data = data.replace(original[key].encode(), neutral[key].encode())
            if name == 'jenkins/template-bindings.json':
                for key, value in original_bindings['credentials'].items():
                    data = data.replace(value.encode(), public_profile['credentials'][key].encode())
        try:
            data.decode('utf-8')
        except UnicodeDecodeError:
            raise ValueError('源码包只接受 UTF-8 文本：' + name) from None
        if b'\0' in data:
            raise ValueError('源码包拒绝二进制内容：' + name)
        if SECRET.search(data):
            raise ValueError('源码包发现疑似私钥；未输出内容：' + name)
        entries[name] = data
    approval_name = 'jenkins/controller/init.groovy.d/03-configure-script-approvals.groovy'
    if public_profile and approval_name in entries:
        hashes = []
        for spec in JOB_SPECS:
            text = entries['jenkins/local/' + spec.template_name].decode().replace('__JOB_NAME__', spec.full_name)
            script = ET.fromstring(text).findtext('.//org.biouno.unochoice.CascadeChoiceParameter/script/secureScript/script')
            hashes.append('SHA512:' + hashlib.sha512(('groovy:' + script).encode()).hexdigest())
        block = '// BEGIN MANAGED SELECTOR HASHES\n[\n' + ''.join("    '{}',\n".format(value) for value in hashes)
        block += '].each { approval.approveScript(it) }\n// END MANAGED SELECTOR HASHES'
        approval, count = re.subn(r'// BEGIN MANAGED SELECTOR HASHES\n.*?// END MANAGED SELECTOR HASHES',
                                  lambda match: block, entries[approval_name].decode(), flags=re.DOTALL)
        if count != 1:
            raise ValueError('公开选择器审批缺少唯一的摘要生成段')
        entries[approval_name] = approval.encode()
    for name in ('LICENSE', 'NOTICE', 'THIRD_PARTY.md', 'SECURITY.md', 'examples/second-project/project.json'):
        if name not in entries:
            raise ValueError('公开发行入口缺失：' + name)
    # 清单只包含路径和摘要，不包含文件内容、账号秘密或 Git 历史。
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in entries.items()}
    revision = subprocess.run(['git', '-C', str(root), 'rev-parse', '--verify', 'HEAD'], capture_output=True, text=True)
    commit = revision.stdout.strip() if revision.returncode == 0 else None
    entries['SOURCE-ORIGIN.json'] = (json.dumps({'sourceCommit': commit, 'gitHistoryIncluded': False,
        'workingTreeSnapshot': True, 'sourceFileSha256': source_hashes, 'files': manifest}, ensure_ascii=False, indent=2) + '\n').encode()
    manifest['SOURCE-ORIGIN.json'] = hashlib.sha256(entries['SOURCE-ORIGIN.json']).hexdigest()
    entries['SOURCE-MANIFEST.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    owned = None
    try:
        with output.open('xb') as raw:
            created = os.fstat(raw.fileno())
            owned = created.st_dev, created.st_ino
            with tarfile.open(fileobj=raw, mode='w:gz') as archive:
                for name, data in entries.items():
                    info = tarfile.TarInfo(name)
                    info.size, info.mode, info.mtime = len(data), 0o755 if name in executable else 0o644, 0
                    archive.addfile(info, io.BytesIO(data))
    except Exception:
        if owned is not None:
            try:
                current = output.lstat()
                if (current.st_dev, current.st_ino) == owned:
                    output.unlink()
            except FileNotFoundError:
                pass
        raise
    return {'files': len(manifest), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'companyProfiles': 0, 'gitHistory': False, 'published': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(export(args.output), ensure_ascii=False))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
