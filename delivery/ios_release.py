#!/usr/bin/env python3
"""编排 iOS IPA 元数据校验、蒲公英上传、App Store 记录和钉钉通知。"""

import argparse
import json
import os
import plistlib
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project import SOURCE_ROOT, platform_config, ios_build_contract
from delivery.changelog import ChangelogError, generate_changelog, git_value
from delivery.delivery import (
    UI_ENGINES,
    VALID_BRAND_NAME,
    ArtifactInfo,
    artifact_sha256,
    DeliveryContext,
    DeliveryContextError,
    optional_zentao_work_item,
    read_json_object,
    resolve_git_target,
    write_json_object,
    validate_ui_engine,
)
from delivery.dingtalk_notify import (
    BrandConfig,
    DingTalkError,
    build_markdown,
    load_brand_config,
    send_release_notification,
)
from delivery.pgyer_upload import (
    PgyerError,
    upload_artifact,
    write_release_info,
)


REPO_ROOT = SOURCE_ROOT
DEFAULT_CONFIG = REPO_ROOT / "ci" / "brands.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "build" / "ci-ios"
TEST_CHANNEL_DISTRIBUTIONS = {
    "pgyer": "adhoc",
    "testflight": "appstore",
}


class IosReleaseError(RuntimeError):
    """iOS 发布输入、产物或配置不满足流水线约束。"""


class IosReleaseTarget(NamedTuple):
    ref: str
    version: Optional[str]
    environment: str
    distribution: str
    delivery: str


class IosBrandConfig(NamedTuple):
    key: str
    scheme: str
    bundle_id: str
    pipeline: str
    environment: str
    distribution: str
    xcconfig: str
    team_key: str
    team_id: str
    team_credential_namespace: str
    repository_signing_path: str
    profile_name: str
    distribution_p12_credential_id: str
    distribution_p12_password_credential_id: str
    asc_api_key_credential_id: str
    asc_key_id_credential_id: str
    asc_issuer_id_credential_id: str
    ui_engine: str = "cmp"
    signing_source: str = "repository"


class IpaMetadata(NamedTuple):
    bundle_id: str
    version_name: str
    build_number: str
    display_name: str


def _credential_id(namespace: str, suffix: str) -> str:
    return "{}-{}".format(namespace, suffix)


def _test_channel(pipeline: str, channel: str) -> str:
    value = channel.strip()
    if pipeline == "release":
        if value:
            raise IosReleaseError("iOS 正式发布不接受测试分发渠道：{}".format(value))
        return ""
    value = value or "pgyer"
    if value not in TEST_CHANNEL_DISTRIBUTIONS:
        raise IosReleaseError("iOS 测试分发渠道必须为 pgyer 或 testflight：{}".format(value))
    return value


def load_ios_brand_config(
    config_path: Path,
    brand: str,
    pipeline: str,
    channel: str = "",
    ui_engine: str = "cmp",
) -> IosBrandConfig:
    validate_ui_engine(ui_engine)
    adapter = platform_config("ios")
    try:
        root = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IosReleaseError("无法读取品牌配置：{}".format(config_path)) from error
    brand_value = root.get(brand) if isinstance(root, dict) else None
    ios_value = brand_value.get("ios") if isinstance(brand_value, dict) else None
    if not isinstance(ios_value, dict):
        raise IosReleaseError("品牌 {} 尚未配置 iOS 构建入口".format(brand))
    if pipeline not in ("test", "release"):
        raise IosReleaseError("iOS 签名类型必须为 test 或 release：{}".format(pipeline))
    scheme = str(ios_value.get("scheme") or "").strip()
    if scheme and ui_engine == "kuikly":
        scheme = ("Kangmin" if scheme == "kangmin" else scheme) + "Kuikly"
    bundle_id = str(ios_value.get("bundleId") or "").strip()
    build_values = ios_value.get("build")
    build_value = (
        build_values.get(pipeline)
        if isinstance(build_values, dict)
        else None
    )
    if not isinstance(build_value, dict):
        raise IosReleaseError(
            "品牌 {} 尚未配置 iOS {} 构建环境".format(brand, pipeline)
        )
    test_channel = _test_channel(pipeline, channel)
    environment = str(build_value.get("environment") or "").strip()
    distribution = (
        TEST_CHANNEL_DISTRIBUTIONS[test_channel]
        if pipeline == "test"
        else str(build_value.get("distribution") or "").strip()
    )
    xcconfig = str(build_value.get("xcconfig") or "").strip()
    expected_environment = "dev" if pipeline == "test" else "prod"
    if environment != expected_environment or (
        pipeline == "release" and distribution != "appstore"
    ):
        raise IosReleaseError(
            "品牌 {} 的 iOS {} 必须使用环境 {}{}".format(
                brand,
                pipeline,
                expected_environment,
                " 和分发方式 appstore" if pipeline == "release" else "",
            )
        )
    if adapter["adapter"] == "sxmqlive":
        valid_xcconfig = re.fullmatch(r"iosApp/Configuration/[A-Za-z0-9][A-Za-z0-9._-]*\.xcconfig", xcconfig)
    else:
        declared = adapter.get("xcconfig", {})
        valid_xcconfig = xcconfig in (declared.values() if isinstance(declared, dict) else [declared])
        if scheme != ios_build_contract(ui_engine)["scheme"] or bundle_id != adapter.get("bundleId"):
            raise IosReleaseError("iOS scheme 或 bundleId 与可信项目配置不一致")
    if not valid_xcconfig:
        raise IosReleaseError(
            "品牌 {} 的 iOS {} xcconfig 路径无效".format(brand, pipeline)
        )
    signing_values = ios_value.get("signing")
    signing_value = (
        signing_values.get(pipeline)
        if isinstance(signing_values, dict)
        else None
    )
    if not isinstance(signing_value, dict):
        raise IosReleaseError(
            "品牌 {} 尚未配置 iOS {} 签名入口".format(brand, pipeline)
        )
    if signing_value.get("enabled") is not True:
        reason = str(signing_value.get("blockReason") or "未完成负责人确认").strip()
        raise IosReleaseError(
            "品牌 {} 的 iOS {} 发布已禁用：{}".format(brand, pipeline, reason)
        )
    team_key = str(signing_value.get("team") or "").strip()
    teams_value = root.get("_iosTeams") if isinstance(root, dict) else None
    team_value = teams_value.get(team_key) if isinstance(teams_value, dict) else None
    if not isinstance(team_value, dict):
        raise IosReleaseError(
            "品牌 {} 的 iOS {} Developer Team {} 未登记".format(
                brand, pipeline, team_key or "<empty>"
            )
        )
    team_id = str(team_value.get("teamId") or "").strip()
    team_namespace = str(team_value.get("credentialNamespace") or "").strip()
    repository_signing_path = str(
        team_value.get("repositorySigningPath") or ""
    ).strip()
    signing_source = (
        str(team_value.get("testSigningSource", "repository")).strip()
        if pipeline == "test" else "jenkins"
    )
    if signing_source not in ("repository", "jenkins"):
        raise IosReleaseError("品牌 {} 的测试签名来源必须为 repository 或 jenkins".format(brand))
    if pipeline == "test":
        profile_names = signing_value.get("profileNames")
        profile_name = str(
            profile_names.get(test_channel) if isinstance(profile_names, dict) else ""
        ).strip()
    else:
        profile_name = str(signing_value.get("profileName") or "").strip()
    if not scheme or not bundle_id:
        raise IosReleaseError("品牌 {} 的 iOS scheme 和 bundleId 必须完整".format(brand))
    if not re.fullmatch(r"[a-z][a-z0-9-]*", team_namespace):
        raise IosReleaseError(
            "品牌 {} 的 Team Credential 命名空间无效".format(brand)
        )
    if not re.fullmatch(r"[A-Z0-9]{10}", team_id):
        raise IosReleaseError(
            "品牌 {} 的 iOS {} Team ID 无效：{}".format(
                brand, pipeline, team_id or "<empty>"
            )
        )
    if not re.fullmatch(
        r"signing/ios/[a-z][a-z0-9_-]*",
        repository_signing_path,
    ):
        raise IosReleaseError(
            "品牌 {} 的 iOS {} 仓库签名目录无效".format(brand, pipeline)
        )
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._ -]{0,126}[A-Za-z0-9]",
        profile_name,
    ):
        raise IosReleaseError(
            "品牌 {} 的 iOS {} Profile 名称无效".format(brand, pipeline)
        )
    return IosBrandConfig(
        key=brand,
        scheme=scheme,
        bundle_id=bundle_id,
        pipeline=pipeline,
        environment=environment,
        distribution=distribution,
        xcconfig=xcconfig,
        team_key=team_key,
        team_id=team_id,
        team_credential_namespace=team_namespace,
        repository_signing_path=repository_signing_path,
        profile_name=profile_name,
        distribution_p12_credential_id=_credential_id(
            team_namespace, "distribution-p12"
        ),
        distribution_p12_password_credential_id=_credential_id(
            team_namespace, "distribution-p12-password"
        ),
        asc_api_key_credential_id=_credential_id(team_namespace, "asc-api-key"),
        asc_key_id_credential_id=_credential_id(team_namespace, "asc-key-id"),
        asc_issuer_id_credential_id=_credential_id(team_namespace, "asc-issuer-id"),
        ui_engine=ui_engine,
        signing_source=signing_source,
    )


def resolve_target_brand(
    pipeline: str,
    branch: str = "",
    tag: str = "",
    brand: Optional[str] = None,
) -> str:
    return resolve_git_target(
        pipeline, branch, tag, IosReleaseError,
        brand=brand, tag_infix="-ios-v", message_prefix="iOS ",
    ).brand


def classify_release_target(
    brand: str,
    pipeline: str,
    branch: str = "",
    tag: str = "",
    channel: str = "",
) -> IosReleaseTarget:
    if not VALID_BRAND_NAME.fullmatch(brand):
        raise IosReleaseError("品牌参数格式无效：{}".format(brand))
    test_channel = _test_channel(pipeline, channel)
    target = resolve_git_target(
        "release" if pipeline == "release" else "test", branch, tag, IosReleaseError,
        brand=brand, tag_infix="-ios-v", message_prefix="iOS ",
    )
    if pipeline == "release":
        return IosReleaseTarget(
            ref=target.ref,
            version=target.version,
            environment="prod",
            distribution="appstore",
            delivery="app-store-connect",
        )

    return IosReleaseTarget(
        ref=target.ref,
        version=target.version,
        environment="dev",
        distribution=TEST_CHANNEL_DISTRIBUTIONS[test_channel],
        delivery=test_channel,
    )


def find_ipa(output_root: Path) -> Path:
    candidates = list(output_root.rglob("*.ipa")) if output_root.is_dir() else []
    if not candidates:
        raise IosReleaseError("未在 {} 中找到 IPA".format(output_root))
    candidates.sort(key=lambda path: (path.stat().st_mtime_ns, str(path)), reverse=True)
    if len(candidates) > 1:
        print("警告：找到多个 IPA，将使用最新文件：{}".format(candidates[0]))
    return candidates[0].resolve()


def _read_ipa_info_plist(ipa: Path) -> Dict[str, Any]:
    try:
        with zipfile.ZipFile(str(ipa)) as archive:
            plist_entries = [
                name
                for name in archive.namelist()
                if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name)
            ]
            if len(plist_entries) != 1:
                raise IosReleaseError(
                    "IPA 顶层 App Info.plist 数量异常：{}".format(len(plist_entries))
                )
            value = plistlib.loads(archive.read(plist_entries[0]))
    except (OSError, zipfile.BadZipFile, KeyError, plistlib.InvalidFileException) as error:
        raise IosReleaseError("无法读取 IPA 元数据：{}".format(ipa)) from error
    if not isinstance(value, dict):
        raise IosReleaseError("IPA Info.plist 不是有效字典：{}".format(ipa))
    return value


def read_ipa_metadata(ipa: Path) -> IpaMetadata:
    value = _read_ipa_info_plist(ipa)
    metadata = IpaMetadata(
        bundle_id=str(value.get("CFBundleIdentifier") or "").strip(),
        version_name=str(value.get("CFBundleShortVersionString") or "").strip(),
        build_number=str(value.get("CFBundleVersion") or "").strip(),
        display_name=str(
            value.get("CFBundleDisplayName") or value.get("CFBundleName") or ""
        ).strip(),
    )
    if not metadata.bundle_id or not metadata.version_name or not metadata.build_number:
        raise IosReleaseError("IPA 缺少 Bundle ID、版本号或构建号：{}".format(ipa))
    return metadata


def _plist_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "yes", "1")


def validate_ipa_build_settings(ipa: Path, ios: IosBrandConfig) -> None:
    value = _read_ipa_info_plist(ipa)
    if platform_config("ios")["adapter"] != "sxmqlive":
        # 原生宿主没有本项目 SDK 字段；Bundle、签名和分发验证仍由相同入口执行。
        return
    expected_api_environment = "test" if ios.environment == "dev" else "production"
    expected_debug_tools = ios.environment == "dev"
    expected_sdk_production = ios.environment == "prod"
    actual = {
        "SkillCureBuildType": str(value.get("SkillCureBuildType") or "").strip().lower(),
        "SkillCureIsDebug": _plist_boolean(value.get("SkillCureIsDebug")),
        "SkillCureDefaultApiEnvironment": str(
            value.get("SkillCureDefaultApiEnvironment") or ""
        ).strip().lower(),
        "SkillCureDebugToolsEnabled": _plist_boolean(
            value.get("SkillCureDebugToolsEnabled")
        ),
        "SkillCureSdkProduction": _plist_boolean(value.get("SkillCureSdkProduction")),
    }
    expected = {
        "SkillCureBuildType": "release",
        "SkillCureIsDebug": False,
        "SkillCureDefaultApiEnvironment": expected_api_environment,
        "SkillCureDebugToolsEnabled": expected_debug_tools,
        "SkillCureSdkProduction": expected_sdk_production,
    }
    mismatches = [key for key in expected if actual[key] != expected[key]]
    webhook = str(value.get("SkillCureDingTalkWebhook") or "").strip()
    secret = str(value.get("SkillCureDingTalkSecret") or "").strip()
    if webhook or secret:
        mismatches.append("SkillCureDingTalkCredentials")
    if mismatches:
        raise IosReleaseError(
            "IPA 构建环境与 {} / {} 配置不一致：{}".format(
                ios.environment,
                ios.distribution,
                ", ".join(mismatches),
            )
        )


def validate_ipa_ui_engine(ipa: Path, ui_engine: str) -> None:
    validate_ui_engine(ui_engine)
    try:
        with zipfile.ZipFile(ipa) as archive:
            app_plists = [name for name in archive.namelist()
                          if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name)]
            if len(app_plists) != 1:
                raise IosReleaseError("IPA 顶层 App Info.plist 数量异常：{}".format(len(app_plists)))
            app_root = app_plists[0].rsplit("/", 1)[0] + "/"
            if platform_config("ios")["adapter"] != "sxmqlive":
                renderer = platform_config("ios").get("renderer")
                if renderer and app_root + renderer not in archive.namelist():
                    raise IosReleaseError("IPA 缺少可信项目声明的 renderer")
                if ui_engine != "native":
                    raise IosReleaseError("原生 Xcode 项目必须使用 native 引擎")
                return
            renderer = app_root + "Frameworks/OpenKuiklyIOSRender.framework/OpenKuiklyIOSRender"
            actual_engine = "kuikly" if renderer in archive.namelist() else "cmp"
    except (OSError, zipfile.BadZipFile) as error:
        raise IosReleaseError("无法读取 IPA 宿主引擎：{}".format(ipa)) from error
    if actual_engine != ui_engine:
        raise IosReleaseError("IPA 实际 UI 引擎 {} 与所选 {} 不一致".format(actual_engine, ui_engine))


def validate_ipa(
    ipa: Path,
    expected_bundle_id: str,
    expected_version: Optional[str],
) -> IpaMetadata:
    metadata = read_ipa_metadata(ipa)
    if metadata.bundle_id != expected_bundle_id:
        raise IosReleaseError(
            "iOS 配置 Bundle ID {} 与 IPA {} 不一致".format(
                expected_bundle_id, metadata.bundle_id
            )
        )
    if expected_version and metadata.version_name != expected_version:
        raise IosReleaseError(
            "Git 版本 {} 与 IPA version {} 不一致，请先更新品牌 xcconfig".format(
                expected_version, metadata.version_name
            )
        )
    return metadata


def validate_release_target(args: argparse.Namespace) -> int:
    channel = getattr(args, "channel", "")
    target = classify_release_target(
        args.brand, args.pipeline, args.branch, args.tag, channel
    )
    ios = load_ios_brand_config(
        args.config.resolve(), args.brand, args.pipeline, channel, getattr(args, "ui_engine", "cmp")
    )
    print("Jenkins Job：iOS {}".format(args.pipeline))
    print("Git 引用：{}".format(target.ref))
    print("版本目标：{}".format(target.version or "由 IPA version 决定"))
    print("Xcode Scheme：{}".format(ios.scheme))
    print("Bundle ID：{}".format(ios.bundle_id))
    print("编译环境：{} ({})".format(ios.environment, ios.xcconfig))
    print("Developer Team：{} ({})".format(ios.team_key, ios.team_id))
    print("签名分发：{}".format(ios.distribution))
    print("发布渠道：{}".format(target.delivery))
    return 0


def print_target_brand(args: argparse.Namespace) -> int:
    brand = resolve_target_brand(args.pipeline, args.branch, args.tag, getattr(args, "brand", None))
    load_ios_brand_config(
        args.config.resolve(), brand, args.pipeline, getattr(args, "channel", ""),
        getattr(args, "ui_engine", "") or os.environ.get("CI_UI_ENGINE") or ("native" if platform_config("ios")["adapter"] == "xcode" else "cmp")
    )
    print(brand)
    return 0


def print_brand_config(args: argparse.Namespace) -> int:
    ios = load_ios_brand_config(
        args.config.resolve(), args.brand, args.pipeline, getattr(args, "channel", ""), getattr(args, "ui_engine", "cmp")
    )
    values = {
        "scheme": ios.scheme,
        "ui-engine": ios.ui_engine,
        "bundle-id": ios.bundle_id,
        "environment": ios.environment,
        "distribution": ios.distribution,
        "xcconfig": ios.xcconfig,
        "team-id": ios.team_id,
        "team-key": ios.team_key,
        "repository-signing-path": ios.repository_signing_path,
        "signing-source": ios.signing_source,
        "profile-name": ios.profile_name,
        "distribution-p12-credential": ios.distribution_p12_credential_id,
        "distribution-p12-password-credential": (
            ios.distribution_p12_password_credential_id
        ),
        "asc-api-key-credential": ios.asc_api_key_credential_id,
        "asc-key-id-credential": ios.asc_key_id_credential_id,
        "asc-issuer-id-credential": ios.asc_issuer_id_credential_id,
    }
    if getattr(args, "format", None) == "json":
        print(json.dumps(values, ensure_ascii=False))
    else:
        print(values[args.field])
    return 0


def prepare_release(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    channel = getattr(args, "channel", "")
    ios = load_ios_brand_config(
        args.config.resolve(), args.brand, args.pipeline, channel, getattr(args, "ui_engine", "cmp")
    )
    brand = load_brand_config(args.config.resolve(), args.brand)
    branch = args.branch or git_value(repo, ["rev-parse", "--abbrev-ref", "HEAD"], "unknown")
    target = classify_release_target(
        args.brand, args.pipeline, branch, args.tag, channel
    )
    ipa = args.ipa.resolve() if args.ipa else find_ipa(args.output_dir.resolve() / "export")
    if not ipa.is_file():
        raise IosReleaseError("IPA 文件不存在：{}".format(ipa))
    metadata = validate_ipa(ipa, ios.bundle_id, target.version)
    validate_ipa_build_settings(ipa, ios)
    validate_ipa_ui_engine(ipa, ios.ui_engine)
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
        platform="ios",
        brand=args.brand,
        display_name=brand.display_name,
        environment=ios.environment,
        branch=target.ref,
        commit=args.commit or git_value(repo, ["rev-parse", "HEAD"], "unknown"),
        builder=args.builder or "jenkins",
        version_name=metadata.version_name,
        version_code=metadata.build_number,
        artifact_path=str(ipa),
        changelog_path=str(changelog_path),
        distribution=ios.distribution,
        delivery=target.delivery,
        ui_engine=ios.ui_engine,
        work_item=optional_zentao_work_item(
            getattr(args, "work_item_type", ""),
            getattr(args, "work_item_id", ""),
            getattr(args, "work_item_url", ""),
        ),
    )
    context = delivery.to_dict(
        {
            "scheme": ios.scheme,
            "bundle_id": metadata.bundle_id,
            "build_type": "Release",
            "version_target": target.version or metadata.version_name,
            "build_number": metadata.build_number,
            "ipa_path": str(ipa),
        }
    )
    write_json_object(output_dir / "context.json", context)
    print("找到 IPA：{}".format(ipa))
    print("版本：{} ({})".format(metadata.version_name, metadata.build_number))
    print("Bundle ID：{}".format(metadata.bundle_id))
    print("编译环境：{}".format(ios.environment))
    print("签名分发：{}".format(ios.distribution))
    print("发布渠道：{}".format(target.delivery))
    print("本次更新日志：\n{}".format(changelog))
    return 0


def _load_context(args: argparse.Namespace):
    context = read_json_object(args.context.resolve(), "iOS 发布上下文")
    try:
        delivery = DeliveryContext.from_dict(context)
    except DeliveryContextError as error:
        raise IosReleaseError(str(error)) from error
    brand = load_brand_config(args.config.resolve(), delivery.brand)
    ipa = Path(delivery.artifact_path)
    changelog_path = Path(delivery.changelog_path)
    if not ipa.is_file():
        raise IosReleaseError("iOS 发布上下文中的 IPA 不存在：{}".format(ipa))
    if not changelog_path.is_file():
        raise IosReleaseError("iOS 发布上下文中的更新日志不存在：{}".format(changelog_path))
    validate_ipa_ui_engine(ipa, delivery.ui_engine)
    return context, delivery, brand, ipa, changelog_path.read_text(encoding="utf-8").strip()


def upload_test_release(args: argparse.Namespace) -> int:
    context, delivery, brand, ipa, changelog = _load_context(args)
    if delivery.delivery != "pgyer":
        raise IosReleaseError("只有 iOS 测试包允许上传蒲公英")
    if args.dry_run:
        release = ArtifactInfo(
            name=brand.display_name,
            version=delivery.version_name or "DRY-RUN",
            version_code=delivery.version_code or "DRY-RUN",
            package_name=str(context.get("bundle_id") or ""),
            build_key="DRY-RUN",
            download_url="",
            qr_code_url="",
            ui_engine=delivery.ui_engine,
        )
        print("DRY_RUN：不会调用蒲公英；将上传 {}。".format(ipa))
    else:
        release = upload_artifact(
            artifact_path=str(ipa),
            api_key=os.environ.get("PGYER_API_KEY", ""),
            update_description=changelog,
            platform="ios",
            channel_shortcut=brand.channel_for("test"),
            delivery_identity={key: getattr(delivery, key) for key in
                               ("platform", "brand", "ui_engine", "environment", "commit", "branch")},
        )
        release = release._replace(ui_engine=delivery.ui_engine)
    write_release_info(release, args.output.resolve())
    return 0


def record_app_store_upload(args: argparse.Namespace) -> int:
    context, delivery, brand, _ipa, _changelog = _load_context(args)
    if delivery.delivery not in ("app-store-connect", "testflight"):
        raise IosReleaseError("只有 App Store 或 TestFlight 包允许记录上传结果")
    is_testflight = delivery.delivery == "testflight"
    if args.dry_run:
        build_key = "DRY-RUN"
    else:
        build_key = "TESTFLIGHT" if is_testflight else "APP-STORE-CONNECT"
    release = ArtifactInfo(
        name=brand.display_name,
        version=delivery.version_name,
        version_code=delivery.version_code,
        package_name=str(context.get("bundle_id") or ""),
        build_key=build_key,
        download_url="",
        qr_code_url="",
        ui_engine=delivery.ui_engine,
    )
    write_release_info(release, args.output.resolve())
    print("{}：已写入 {} 上传记录。".format(
        "DRY_RUN" if args.dry_run else "UPLOAD",
        "TestFlight" if is_testflight else "App Store Connect",
    ))
    return 0


def notify_release(args: argparse.Namespace) -> int:
    _context, delivery, brand, _ipa, changelog = _load_context(args)
    release = ArtifactInfo.from_dict(
        read_json_object(args.release_info.resolve(), "iOS 发布上下文")
    )
    if release.ui_engine != delivery.ui_engine:
        raise IosReleaseError("iOS 分发记录与发布上下文的 UI 引擎不一致")
    markdown = build_markdown(
        release=release,
        brand=brand,
        changelog=changelog,
        environment=delivery.environment or "test",
        branch=delivery.branch,
        commit=delivery.commit,
        builder=delivery.builder,
        mask_mobiles=True,
        platform="iOS",
        distribution=delivery.delivery,
        work_item=delivery.work_item,
        ui_engine=delivery.ui_engine,
    )
    print("钉钉消息预览：\n{}".format(markdown))
    if args.dry_run:
        print("DRY_RUN：不会调用钉钉 Webhook。")
        return 0
    if not brand.testers or any(not tester.mobile for tester in brand.testers):
        raise IosReleaseError("iOS 安装包已发布，但品牌测试人员手机号 Credential 不完整")
    send_release_notification(
        release=release,
        brand=brand,
        changelog=changelog,
        environment=delivery.environment or "test",
        branch=delivery.branch,
        commit=delivery.commit,
        builder=delivery.builder,
        webhook=os.environ.get("DINGTALK_WEBHOOK", ""),
        secret=os.environ.get("DINGTALK_SECRET", ""),
        platform="iOS",
        distribution=delivery.delivery,
        work_item=delivery.work_item,
        ui_engine=delivery.ui_engine,
        artifact_sha256=artifact_sha256(_ipa),
    )
    print("iOS 钉钉发布通知发送成功。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")

    validate = subparsers.add_parser("validate", help="校验 iOS 分支或正式 Tag")
    validate.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    validate.add_argument("--brand", required=True)
    validate.add_argument("--pipeline", required=True, choices=("test", "release"))
    validate.add_argument("--branch", default="")
    validate.add_argument("--tag", default="")
    validate.add_argument(
        "--channel", choices=tuple(TEST_CHANNEL_DISTRIBUTIONS), default=""
    )
    validate.set_defaults(action=validate_release_target)

    resolve_brand = subparsers.add_parser("resolve-brand", help="从 iOS Git 引用解析品牌")
    resolve_brand.add_argument("--brand", default=None)
    resolve_brand.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    resolve_brand.add_argument("--pipeline", required=True, choices=("test", "release"))
    resolve_brand.add_argument("--branch", default="")
    resolve_brand.add_argument("--tag", default="")
    resolve_brand.add_argument(
        "--channel", choices=tuple(TEST_CHANNEL_DISTRIBUTIONS), default=""
    )
    resolve_brand.set_defaults(action=print_target_brand)

    config = subparsers.add_parser("config", help="输出 iOS 品牌构建字段")
    config.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    config.add_argument("--brand", required=True)
    config.add_argument("--pipeline", required=True, choices=("test", "release"))
    config.add_argument(
        "--channel", choices=tuple(TEST_CHANNEL_DISTRIBUTIONS), default=""
    )
    config_output = config.add_mutually_exclusive_group(required=True)
    config_output.add_argument("--format", choices=("json",))
    config_output.add_argument(
        "--field",
        choices=(
            "scheme",
            "ui-engine",
            "bundle-id",
            "environment",
            "distribution",
            "xcconfig",
            "team-id",
            "team-key",
            "repository-signing-path",
            "signing-source",
            "profile-name",
            "distribution-p12-credential",
            "distribution-p12-password-credential",
            "asc-api-key-credential",
            "asc-key-id-credential",
            "asc-issuer-id-credential",
        ),
    )
    config.set_defaults(action=print_brand_config)

    prepare = subparsers.add_parser("prepare", help="校验 IPA 并生成 iOS 发布上下文")
    prepare.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    prepare.add_argument("--repo", type=Path, default=REPO_ROOT)
    prepare.add_argument("--brand", required=True)
    prepare.add_argument("--pipeline", required=True, choices=("test", "release"))
    prepare.add_argument("--ipa", type=Path)
    prepare.add_argument("--manual-changelog", default="")
    prepare.add_argument("--last-successful-commit", default="")
    prepare.add_argument("--branch", default="")
    prepare.add_argument("--tag", default="")
    prepare.add_argument(
        "--channel", choices=tuple(TEST_CHANNEL_DISTRIBUTIONS), default=""
    )
    prepare.add_argument("--commit", default="")
    prepare.add_argument("--builder", default="")
    prepare.add_argument("--work-item-type", default="")
    prepare.add_argument("--work-item-id", default="")
    prepare.add_argument("--work-item-url", default="")
    prepare.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    prepare.set_defaults(action=prepare_release)
    for command in (validate, config, prepare):
        command.add_argument("--ui-engine", choices=UI_ENGINES,
                             default=os.environ.get("CI_UI_ENGINE", "cmp"))

    upload = subparsers.add_parser("upload", help="上传 iOS 测试 IPA 到蒲公英")
    upload.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    upload.add_argument("--context", type=Path, default=DEFAULT_OUTPUT_DIR / "context.json")
    upload.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR / "release-info.json")
    upload.add_argument("--dry-run", action="store_true")
    upload.set_defaults(action=upload_test_release)

    record = subparsers.add_parser("record-app-store", help="记录 App Store Connect 上传结果")
    record.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    record.add_argument("--context", type=Path, default=DEFAULT_OUTPUT_DIR / "context.json")
    record.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR / "release-info.json")
    record.add_argument("--dry-run", action="store_true")
    record.set_defaults(action=record_app_store_upload)

    notify = subparsers.add_parser("notify", help="预览或发送 iOS 钉钉通知")
    notify.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
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
        parser.error(
            "必须指定 validate、resolve-brand、config、prepare、upload、record-app-store 或 notify"
        )
    try:
        return int(args.action(args))
    except (
        IosReleaseError,
        DeliveryContextError,
        ChangelogError,
        PgyerError,
        DingTalkError,
    ) as error:
        print("iOS 发布流程失败：{}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
