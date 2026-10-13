#!/usr/bin/env python3
"""编排 Android 更新日志、蒲公英上传和钉钉通知。"""

import argparse
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import NamedTuple, Optional

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project import SOURCE_ROOT, application_config_module, android_contract, platform_config

from delivery.changelog import ChangelogError, generate_changelog, git_value
from delivery.delivery import (
    VALID_BRAND_NAME,
    UI_ENGINES,
    validate_ui_engine,
    ArtifactInfo,
    artifact_sha256,
    DeliveryContext,
    DeliveryContextError,
    optional_zentao_work_item,
    read_json_object,
    resolve_git_target,
    write_json_object,
)
from delivery.dingtalk_notify import (
    BrandConfig,
    DingTalkError,
    build_markdown,
    load_brand_config,
    send_release_notification,
)
from delivery.pgyer_upload import PgyerError, upload_artifact, write_release_info


class IosProjectConfigurationError(ValueError):
    pass


def load_android_brand_specs(path):
    module = application_config_module()
    try:
        return module.load_android_brand_specs(path)
    except module.IosProjectConfigurationError as error:
        raise IosProjectConfigurationError(str(error)) from error

REPO_ROOT = SOURCE_ROOT
DEFAULT_CONFIG = REPO_ROOT / "ci" / "brands.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "build" / "ci"
BUILD_TYPES = {
    "test": "Qa",
    "release": "Release",
}


class ReleaseError(RuntimeError):
    """发布编排输入或本地状态无效。"""


class ReleaseTarget(NamedTuple):
    """由 Git 引用确定的版本发布目标。"""

    ref: str
    version: Optional[str]
    environment: str


def build_type(pipeline: str) -> str:
    """返回流水线唯一允许的 Android 构建类型。"""
    try:
        return platform_config("android").get("buildTypes", BUILD_TYPES)[pipeline]
    except KeyError as error:
        raise ReleaseError(
            "Jenkins Job 类型必须为 test 或 release：{}".format(pipeline)
        ) from error


def gradle_task(brand: str, pipeline: str, ui_engine: str = "cmp") -> str:
    if not VALID_BRAND_NAME.fullmatch(brand):
        raise ReleaseError("品牌参数格式无效：{}".format(brand))
    contract = android_contract(brand, pipeline, ui_engine)
    return ":{}:assemble{}".format(contract["module"].replace("/", ":"), contract["variant"])


def resolve_target_brand(
    pipeline: str,
    branch: str = "",
    tag: str = "",
    brand: Optional[str] = None,
) -> str:
    """从测试分支或正式 Tag 中解析唯一的品牌。"""
    return resolve_git_target(pipeline, branch, tag, ReleaseError, brand=brand).brand


def classify_release_target(
    brand: str,
    pipeline: str,
    branch: str = "",
    tag: str = "",
) -> ReleaseTarget:
    """测试 Job 接受品牌开发分支，正式 Job 只接受品牌版本 Tag。"""
    if not VALID_BRAND_NAME.fullmatch(brand):
        raise ReleaseError("品牌参数格式无效：{}".format(brand))

    environment = "release" if pipeline == "release" else "test"
    target = resolve_git_target(environment, branch, tag, ReleaseError, brand=brand)
    return ReleaseTarget(ref=target.ref, version=target.version, environment=environment)


def read_apk_metadata(metadata: Path) -> dict:
    try:
        value = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseError("无法读取 APK 元数据：{}".format(metadata)) from error
    if not isinstance(value, dict):
        raise ReleaseError("APK 元数据必须为对象：{}".format(metadata))
    return value


def validate_apk_identity(repo: Path, apk: Path, brand: str, pipeline: str, ui_engine: str = "cmp") -> None:
    contract = android_contract(brand, pipeline, ui_engine)
    expected_dir = repo / contract["outputDir"]
    if apk.parent.resolve() != expected_dir.resolve():
        raise ReleaseError("APK 目录与所选 UI 引擎、品牌或构建类型不一致：{}".format(expected_dir))
    value = read_apk_metadata(repo / contract["metadata"])
    if contract["adapter"] == "sxmqlive":
        plugin = repo / "build-logic/src/main/kotlin/com/skillcureacu/buildlogic/BrandConfigurationPlugin.kt"
        try:
            specs = load_android_brand_specs(plugin)
        except (IosProjectConfigurationError, ValueError) as error:
            raise ReleaseError(str(error)) from error
        if brand not in specs:
            raise ReleaseError("Android BrandSpec 未包含品牌：{}".format(brand))
        expected_id = specs[brand]["applicationId"]
    else:
        expected_id = contract["applicationId"]
        if not expected_id:
            raise ReleaseError("Gradle 项目必须声明 applicationId")
    expected_variant = contract["variant"][0].lower() + contract["variant"][1:]
    if value.get("variantName") != expected_variant:
        raise ReleaseError("APK 变体与 {} 不一致".format(expected_variant))
    if value.get("applicationId") != expected_id:
        raise ReleaseError("APK applicationId 与当前品牌不一致")
    elements = value.get("elements")
    if not isinstance(elements, list) or len(elements) != 1 or not isinstance(elements[0], dict):
        raise ReleaseError("APK 元数据必须声明唯一产物")
    element = elements[0]
    filename = element.get("outputFile")
    if not isinstance(filename, str) or not filename or Path(filename).name != filename or filename in (".", ".."):
        raise ReleaseError("APK outputFile 必须为直接文件名")
    if Path(filename).suffix.lower() != ".apk":
        raise ReleaseError("APK outputFile 必须为 .apk 文件")
    if element.get("filters") != []:
        raise ReleaseError("APK 发布不支持分包 filters")
    if filename != apk.name or not apk.is_file() or apk.is_symlink() or repo.resolve() not in apk.resolve().parents:
        raise ReleaseError("APK 与元数据声明的产物不一致或文件不存在")
    if contract["artifact"].endswith(".apk") and (repo / contract["artifact"]).resolve() != apk.resolve():
        raise ReleaseError("APK 不属于可信项目产物声明")
    if contract["adapter"] != "sxmqlive":
        try:
            with zipfile.ZipFile(apk) as archive:
                if archive.namelist().count("AndroidManifest.xml") != 1:
                    raise ReleaseError("APK 必须包含唯一 AndroidManifest.xml")
        except (OSError, zipfile.BadZipFile) as error:
            raise ReleaseError("APK 容器损坏") from error
        return
    try:
        with zipfile.ZipFile(str(apk)) as archive:
            packaged_engine = archive.read("assets/ui-engine.txt").decode("utf-8").strip()
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError) as error:
        raise ReleaseError("无法读取 APK UI 引擎标识：{}".format(apk)) from error
    if packaged_engine != ui_engine:
        raise ReleaseError("APK UI 引擎 {} 与所选 {} 不一致".format(packaged_engine, ui_engine))


def find_apk(repo: Path, brand: str, pipeline: str, ui_engine: str = "cmp") -> Path:
    # AGP 元数据是唯一产物入口，不能以旧文件的 mtime 或模糊品牌目录猜测。
    contract = android_contract(brand, pipeline, ui_engine)
    output_dir = repo / contract["outputDir"]
    value = read_apk_metadata(repo / contract["metadata"])
    elements = value.get("elements")
    if not isinstance(elements, list) or len(elements) != 1 or not isinstance(elements[0], dict):
        raise ReleaseError("APK 元数据必须声明唯一产物")
    filename = elements[0].get("outputFile")
    if not isinstance(filename, str) or not filename or Path(filename).name != filename or filename in (".", ".."):
        raise ReleaseError("APK outputFile 必须为直接文件名")
    apk = output_dir / filename
    validate_apk_identity(repo, apk, brand, pipeline, ui_engine)
    return apk.resolve()


def read_apk_version(apk: Path) -> str:
    """从 Android Gradle Plugin 产物元数据读取 APK 的 versionName。"""
    metadata = apk.parent / "output-metadata.json"
    value = read_apk_metadata(metadata)
    elements = value.get("elements") if isinstance(value, dict) else None
    if not isinstance(elements, list):
        raise ReleaseError("APK 版本元数据缺少 elements：{}".format(metadata))
    for element in elements:
        if not isinstance(element, dict) or element.get("outputFile") != apk.name:
            continue
        version_name = str(element.get("versionName") or "").strip()
        if version_name:
            return version_name
    raise ReleaseError("APK 版本元数据中未找到产物 {}".format(apk.name))


def validate_apk_version(apk: Path, expected_version: str) -> str:
    version_name = read_apk_version(apk)
    if version_name != expected_version:
        raise ReleaseError(
            "Git 版本 {} 与 APK versionName {} 不一致，请先更新 BrandSpec".format(
                expected_version, version_name
            )
        )
    return version_name


def prepare_release(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    ui_engine = validate_ui_engine(getattr(args, "ui_engine", "cmp"))
    brand_config = load_brand_config(args.config.resolve(), args.brand)
    branch = args.branch or git_value(repo, ["rev-parse", "--abbrev-ref", "HEAD"], "unknown")
    target = classify_release_target(args.brand, args.pipeline, branch, args.tag)
    selected_build_type = build_type(args.pipeline)
    task = gradle_task(args.brand, args.pipeline, ui_engine)
    apk = (
        args.apk.resolve()
        if args.apk
        else find_apk(repo, args.brand, args.pipeline, ui_engine)
    )
    if not apk.is_file():
        raise ReleaseError("APK 文件不存在：{}".format(apk))
    validate_apk_identity(repo, apk, args.brand, args.pipeline, ui_engine)
    version_name = (
        validate_apk_version(apk, target.version)
        if target.version
        else read_apk_version(apk)
    )
    changelog = generate_changelog(
        repo=repo,
        manual_description=args.manual_changelog,
        last_successful_commit=args.last_successful_commit,
    )
    output_dir = args.output_dir.resolve()
    changelog_path = output_dir / "changelog.md"
    changelog_path.parent.mkdir(parents=True, exist_ok=True)
    changelog_path.write_text(changelog + "\n", encoding="utf-8")

    delivery = DeliveryContext(
        platform="android",
        brand=args.brand,
        display_name=brand_config.display_name,
        environment=target.environment,
        branch=target.ref,
        commit=args.commit or git_value(repo, ["rev-parse", "HEAD"], "unknown"),
        builder=args.builder or "jenkins",
        version_name=version_name,
        version_code="",
        artifact_path=str(apk),
        changelog_path=str(changelog_path),
        distribution="",
        delivery="pgyer",
        ui_engine=ui_engine,
        work_item=optional_zentao_work_item(
            getattr(args, "work_item_type", ""),
            getattr(args, "work_item_id", ""),
            getattr(args, "work_item_url", ""),
        ),
    )
    context = delivery.to_dict(
        {
            "build_type": selected_build_type,
            "version_target": target.version or version_name,
            "gradle_task": task,
            "apk_path": str(apk),
        }
    )
    write_json_object(output_dir / "context.json", context)
    print("Gradle Variant：{}".format(task))
    print("UI 引擎：{}".format(ui_engine))
    print("找到 APK：{}".format(apk))
    print("本次更新日志：\n{}".format(changelog))
    print("版本目标：{}".format(target.version or version_name))
    print("APK versionName：{}".format(version_name))
    print("发布环境：{}".format(target.environment))
    print("将上传到蒲公英：{}".format(apk.name))
    tester_names = [item.name for item in brand_config.testers]
    print("将 @ 测试人员：{}".format("、".join(tester_names) if tester_names else "未配置"))
    return 0


def validate_release_target(args: argparse.Namespace) -> int:
    ui_engine = validate_ui_engine(getattr(args, "ui_engine", "cmp"))
    target = classify_release_target(args.brand, args.pipeline, args.branch, args.tag)
    print("Jenkins Job：{}".format(args.pipeline))
    print("Git 引用：{}".format(target.ref))
    print("版本目标：{}".format(target.version or "由 APK versionName 决定"))
    print("发布环境：{}".format(target.environment))
    print("Gradle Variant：{}".format(gradle_task(args.brand, args.pipeline, ui_engine)))
    print("UI 引擎：{}".format(ui_engine))
    return 0


def print_target_brand(args: argparse.Namespace) -> int:
    print(resolve_target_brand(args.pipeline, args.branch, args.tag, getattr(args, "brand", None)))
    return 0


def _context_paths(context: DeliveryContext) -> tuple:
    apk = Path(context.artifact_path)
    changelog_path = Path(context.changelog_path)
    if not apk.is_file():
        raise ReleaseError("发布上下文中的 APK 不存在：{}".format(apk))
    if not changelog_path.is_file():
        raise ReleaseError("发布上下文中的更新日志不存在：{}".format(changelog_path))
    return apk, changelog_path


def _load_context_brand(args: argparse.Namespace):
    context = read_json_object(args.context.resolve())
    try:
        delivery = DeliveryContext.from_dict(context)
    except DeliveryContextError as error:
        raise ReleaseError(str(error)) from error
    brand_config = load_brand_config(args.config.resolve(), delivery.brand)
    apk, changelog_path = _context_paths(delivery)
    changelog = changelog_path.read_text(encoding="utf-8").strip()
    return delivery, brand_config, apk, changelog


def upload_release(args: argparse.Namespace) -> int:
    context, brand, apk, changelog = _load_context_brand(args)
    environment = context.environment or "test"
    if args.dry_run:
        release = ArtifactInfo(
            name=brand.display_name,
            version="DRY-RUN",
            version_code="DRY-RUN",
            package_name="由蒲公英解析",
            build_key="DRY-RUN",
            download_url="",
            qr_code_url="",
        )
        print("DRY_RUN：不会调用蒲公英；将上传 {}。".format(apk))
    else:
        api_key = os.environ.get("PGYER_API_KEY", "")
        release = upload_artifact(
            artifact_path=str(apk),
            platform="android",
            api_key=api_key,
            update_description=changelog,
            install_password=None,
            channel_shortcut=brand.channel_for(environment),
            delivery_identity={key: getattr(context, key) for key in
                               ("platform", "brand", "ui_engine", "environment", "commit", "branch")},
        )
    release = release._replace(ui_engine=context.ui_engine)
    write_release_info(release, args.output.resolve())
    print("蒲公英发布信息：版本 {} ({})，buildKey {}".format(
        release.version, release.version_code, release.build_key
    ))
    if release.download_url:
        print("下载页面：{}".format(release.download_url))
    return 0


def notify_release(args: argparse.Namespace) -> int:
    context, brand, _apk, changelog = _load_context_brand(args)
    release_value = read_json_object(args.release_info.resolve())
    release = ArtifactInfo.from_dict(release_value)
    if release.ui_engine != context.ui_engine:
        raise ReleaseError("发布记录 UI 引擎与发布上下文不一致")
    markdown = build_markdown(
        release=release,
        brand=brand,
        changelog=changelog,
        environment=context.environment or "test",
        branch=context.branch,
        commit=context.commit,
        builder=context.builder,
        mask_mobiles=True,
        work_item=context.work_item,
        ui_engine=context.ui_engine,
    )
    print("钉钉消息预览：\n{}".format(markdown))
    if args.dry_run:
        print("DRY_RUN：不会调用钉钉 Webhook。")
        return 0
    if not brand.testers or any(not tester.mobile for tester in brand.testers):
        raise ReleaseError(
            "品牌 {} 缺少测试人员手机号 Credential；APK 已发布，但不会发送无 @ 人员的通知".format(
                brand.key
            )
        )
    send_release_notification(
        release=release,
        brand=brand,
        changelog=changelog,
        environment=context.environment or "test",
        branch=context.branch,
        commit=context.commit,
        builder=context.builder,
        webhook=os.environ.get("DINGTALK_WEBHOOK", ""),
        secret=os.environ.get("DINGTALK_SECRET", ""),
        work_item=context.work_item,
        ui_engine=context.ui_engine,
        artifact_sha256=artifact_sha256(_apk),
    )
    print("钉钉发布通知发送成功。")
    return 0


def _add_shared_file_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    # Python 3.6 的 add_subparsers 尚不支持 required 参数，统一在 main 中校验。
    subparsers = parser.add_subparsers(dest="command")

    validate = subparsers.add_parser("validate", help="校验品牌版本分支或正式发布 Tag")
    validate.add_argument("--brand", required=True)
    validate.add_argument("--pipeline", required=True, choices=("test", "release"))
    validate.add_argument("--branch", default="")
    validate.add_argument("--tag", default="")
    validate.add_argument("--ui-engine", choices=UI_ENGINES, default=os.environ.get("CI_UI_ENGINE", "cmp"))
    validate.set_defaults(action=validate_release_target)

    resolve_brand = subparsers.add_parser("resolve-brand", help="从 Git 引用解析品牌")
    resolve_brand.add_argument("--brand", default=None)
    resolve_brand.add_argument("--pipeline", required=True, choices=("test", "release"))
    resolve_brand.add_argument("--branch", default="")
    resolve_brand.add_argument("--tag", default="")
    resolve_brand.set_defaults(action=print_target_brand)

    prepare = subparsers.add_parser("prepare", help="定位 APK 并生成更新日志和发布上下文")
    _add_shared_file_arguments(prepare)
    prepare.add_argument("--repo", type=Path, default=REPO_ROOT)
    prepare.add_argument("--brand", required=True)
    prepare.add_argument("--pipeline", required=True, choices=("test", "release"))
    prepare.add_argument("--ui-engine", choices=UI_ENGINES, default=os.environ.get("CI_UI_ENGINE", "cmp"))
    prepare.add_argument("--apk", type=Path)
    prepare.add_argument("--manual-changelog", default="")
    prepare.add_argument("--last-successful-commit", default="")
    prepare.add_argument("--branch", default="")
    prepare.add_argument("--tag", default="")
    prepare.add_argument("--commit", default="")
    prepare.add_argument("--builder", default="")
    prepare.add_argument("--work-item-type", default="")
    prepare.add_argument("--work-item-id", default="")
    prepare.add_argument("--work-item-url", default="")
    prepare.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    prepare.set_defaults(action=prepare_release)

    upload = subparsers.add_parser("upload", help="上传 APK 并写入蒲公英发布结果")
    _add_shared_file_arguments(upload)
    upload.add_argument("--context", type=Path, default=DEFAULT_OUTPUT_DIR / "context.json")
    upload.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR / "release-info.json")
    upload.add_argument("--dry-run", action="store_true")
    upload.set_defaults(action=upload_release)

    notify = subparsers.add_parser("notify", help="预览或发送钉钉通知")
    _add_shared_file_arguments(notify)
    notify.add_argument("--context", type=Path, default=DEFAULT_OUTPUT_DIR / "context.json")
    notify.add_argument(
        "--release-info", type=Path, default=DEFAULT_OUTPUT_DIR / "release-info.json"
    )
    notify.add_argument("--dry-run", action="store_true")
    notify.set_defaults(action=notify_release)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "action"):
        parser.error("必须指定 validate、resolve-brand、prepare、upload 或 notify")
    try:
        return int(args.action(args))
    except (
        ReleaseError,
        DeliveryContextError,
        ChangelogError,
        PgyerError,
        DingTalkError,
    ) as error:
        print("发布流程失败：{}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
