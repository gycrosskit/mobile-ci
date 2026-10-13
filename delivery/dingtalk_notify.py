#!/usr/bin/env python3
"""构造并发送钉钉加签机器人发布与 Jenkins 异常通知。"""

import base64
import hashlib
import hmac
from http.client import HTTPException
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request

import sys
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delivery.delivery import ArtifactInfo, DELIVERY_RETRY_DELAYS, WorkItemRef, delivery_operation, delivery_namespace, validate_ui_engine
from delivery.delivery import delivery_urlopen as urlopen


class DingTalkError(RuntimeError):
    """钉钉通知失败，错误消息中不得包含 Webhook 或 Secret。"""


class DingTalkRejectedError(DingTalkError):
    """钉钉明确拒绝本次消息，允许有限重试。"""


class Tester(NamedTuple):
    name: str
    mobile: str


class BrandConfig(NamedTuple):
    key: str
    display_name: str
    testers: Sequence[Tester]
    pgyer_channels: Mapping[str, str]

    def channel_for(self, environment: str) -> Optional[str]:
        value = self.pgyer_channels.get(environment, "").strip()
        return value or None


FAILURE_STAGE_SUMMARIES = {
    "Validate Parameters": "构建参数不完整，或品牌与 Git 引用不匹配",
    "Checkout Selected Branch": "业务分支检出失败，请检查分支、网络或 Gitee 凭据",
    "Checkout Selected Tag": "发布 Tag 检出失败，请检查 Tag、网络或 Gitee 凭据",
    "Checkout Master": "master 检出失败，请检查网络或 Gitee 凭据",
    "Checkout Reviewed Configuration": "已评审的 master 配置检出失败",
    "Validate Test Target": "Android 测试目标校验失败",
    "Validate Formal Release Target": "Android 正式发布目标校验失败",
    "Resolve iOS Target": "iOS 测试 Target 或签名配置解析失败",
    "Resolve iOS Release Target": "iOS 正式 Target 或签名配置解析失败",
    "Resolve Brands And Quality Gate": "iOS 设备管理参数、工具链或仓库门禁失败",
    "Quality Gate": "代码测试、Lint、编译预检或依赖门禁失败",
    "Build APK": "Android 单测、Lint 或 APK 构建失败",
    "Validate R8 Outputs": "R8 混淆映射产物缺失",
    "Prepare Temporary Keychain": "iOS 临时 Keychain 创建失败",
    "Install Temporary Signing": "iOS 正式签名材料安装失败",
    "Install Distribution Certificate": "iOS 正式签名证书安装失败",
    "Install Repository Signing Certificate": "iOS 测试签名证书安装失败",
    "Install Test Signing Certificate": "iOS 测试签名证书安装失败",
    "Download Latest Ad Hoc Profile": "Ad Hoc Profile 下载或 Apple API 调用失败",
    "Install Latest Ad Hoc Profile": "Ad Hoc Profile 安装失败",
    "Download Test Profile": "iOS 测试 Profile 下载或 Apple API 调用失败",
    "Install Test Profile": "iOS 测试 Profile 安装失败",
    "Download App Store Profile": "App Store Profile 下载或 Apple API 调用失败",
    "Install App Store Profile": "App Store Profile 安装失败",
    "Archive QA App": "iOS 测试 App Archive 失败",
    "Archive Release App": "iOS 正式 App Archive 失败",
    "Export QA IPA": "iOS 测试 IPA 导出或校验失败",
    "Export Release IPA": "iOS 正式 IPA 导出或校验失败",
    "Verify IPA Signature": "iOS 签名、证书、Profile 或包身份验收失败，已阻断上传",
    "Generate Changelog": "Android 发布上下文或更新日志生成失败",
    "Generate Release Context": "iOS 发布上下文或更新日志生成失败",
    "Prepare Release": "发布上下文或更新日志生成失败",
    "Upload To Pgyer": "蒲公英上传或结果轮询失败",
    "Distribute Test IPA": "iOS 测试包分发失败",
    "Upload To App Store Connect": "App Store Connect 校验或上传失败",
    "Notify DingTalk": "安装包已处理，但发布通知发送失败",
    "Archive QA Artifacts": "Android 测试产物归档失败",
    "Archive Release Artifacts": "Android 正式产物归档失败",
    "Archive iOS QA Artifacts": "iOS 测试产物归档失败",
    "Archive iOS Release Artifacts": "iOS 正式产物归档失败",
    "Manage Apple Devices": "Apple 测试设备查询、注册或 Profile 刷新失败",
    "Archive Device Inventory": "iOS 设备清单归档失败",
    "Cleanup Temporary Signing": "iOS 临时 Keychain 或 Profile 清理失败",
    "Controlled Failure": "管理员主动执行 Jenkins 失败告警验收",
}


def load_brand_config(config_path: Path, brand: str) -> BrandConfig:
    try:
        root = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DingTalkError("无法读取品牌配置：{}".format(config_path)) from error
    value = root.get(brand) if isinstance(root, dict) else None
    if not isinstance(value, dict):
        raise DingTalkError("brands.json 未配置品牌：{}".format(brand))

    defaults = root.get("_defaults", {})
    if not isinstance(defaults, dict):
        raise DingTalkError("brands.json 的 _defaults 必须是对象")
    testers_value = value.get("testers", defaults.get("testers", []))
    if not isinstance(testers_value, list):
        raise DingTalkError("品牌 {} 的 testers 必须是数组".format(brand))
    testers: List[Tester] = []
    for item in testers_value:
        if not isinstance(item, dict):
            raise DingTalkError("品牌 {} 存在无效测试人员配置".format(brand))
        name = str(item.get("name") or "").strip()
        mobile = str(item.get("mobile") or "").strip()
        mobile_env = str(item.get("mobileEnv") or "").strip()
        if mobile and mobile_env:
            raise DingTalkError("品牌 {} 的测试人员不能同时配置 mobile 和 mobileEnv".format(brand))
        if mobile_env:
            mobile = os.environ.get(mobile_env, "").strip()
        if not name or (not mobile and not mobile_env):
            raise DingTalkError("品牌 {} 的测试人员姓名和手机号来源不能为空".format(brand))
        testers.append(Tester(name=name, mobile=mobile))

    channels = value.get("pgyerChannels", {})
    if not isinstance(channels, dict):
        raise DingTalkError("品牌 {} 的 pgyerChannels 必须是对象".format(brand))
    return BrandConfig(
        key=brand,
        display_name=str(value.get("displayName") or brand),
        testers=testers,
        pgyer_channels={str(key): str(channel) for key, channel in channels.items()},
    )


def signed_webhook(webhook: str, secret: str, timestamp_ms: int) -> str:
    if not webhook or not secret:
        raise DingTalkError("缺少 DINGTALK_WEBHOOK 或 DINGTALK_SECRET")
    string_to_sign = "{}\n{}".format(timestamp_ms, secret).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode("ascii")
    parsed = urlsplit(webhook)
    if parsed.scheme != "https" or not parsed.hostname:
        raise DingTalkError("DINGTALK_WEBHOOK 必须是有效的 HTTPS 地址")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend([("timestamp", str(timestamp_ms)), ("sign", signature)])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def build_markdown(
    release: ArtifactInfo,
    brand: BrandConfig,
    changelog: str,
    environment: str,
    branch: str,
    commit: str,
    builder: str,
    mask_mobiles: bool = False,
    platform: str = "Android",
    distribution: str = "pgyer",
    work_item: Optional[WorkItemRef] = None,
    ui_engine: str = "cmp",
) -> str:
    is_formal = environment in ("release", "prod") or distribution == "app-store-connect"
    package_label = "正式发布包" if is_formal else "版本测试包"
    lines = [
        "### 📦 {} {} {}".format(brand.display_name, platform, package_label),
        "",
        "**版本：** {} ({})".format(release.version or "待发布", release.version_code or "待发布"),
        "",
        "**环境：** {}".format(environment.capitalize()),
        "",
        "**UI 引擎：** {}".format({"cmp": "CMP", "kuikly": "Kuikly", "native": "Native"}[validate_ui_engine(ui_engine)]),
        "",
        "**分支：** {}".format(branch or "unknown"),
        "",
        "**Commit：** {}".format((commit or "unknown")[:8]),
        "",
        "**构建人：** {}".format(builder or "jenkins"),
        "",
    ]
    if work_item is not None:
        lines.extend(
            [
                "**关联禅道：** [{}]({})".format(
                    work_item.display_name,
                    work_item.url,
                ),
                "",
            ]
        )
    lines.extend(
        [
            "---",
            "",
            "### 📝 本次更新",
            "",
            changelog.strip() or "- 未填写更新内容",
            "",
            "---",
            "",
        ]
    )
    if distribution in ("app-store-connect", "testflight"):
        title = "TestFlight" if distribution == "testflight" else "App Store Connect"
        message = (
            "测试包已上传，等待 Apple 处理后可在 TestFlight 安装。"
            if distribution == "testflight"
            else "安装包已上传，等待 Apple 处理后可进入 TestFlight 或发布流程。"
        )
        lines.extend(["### 🍎 {}".format(title), "", message, ""])
    else:
        lines.extend(["### 📱 扫码安装", ""])
        if release.qr_code_url:
            lines.extend(["![蒲公英二维码]({})".format(release.qr_code_url), ""])
        if release.download_url:
            lines.extend(["[点击下载安装]({})".format(release.download_url), ""])
        else:
            lines.extend(["下载地址将在蒲公英发布完成后生成。", ""])
    if brand.testers:
        mentions = []
        for item in brand.testers:
            if mask_mobiles:
                mobile = _masked_mobile(item.mobile) if item.mobile else "由 Jenkins Credential 注入"
            else:
                mobile = item.mobile
            mentions.append("@{}（@{}）".format(item.name, mobile))
        lines.append(" ".join(mentions))
    else:
        lines.append("未配置测试人员，请在 ci/brands.json 中补充。")
    return "\n".join(lines)


def _masked_mobile(mobile: str) -> str:
    if len(mobile) <= 7:
        return "*" * len(mobile)
    return "{}****{}".format(mobile[:3], mobile[-4:])


def build_payload(
    markdown: str,
    brand: BrandConfig,
    platform: str = "Android",
) -> Dict[str, Any]:
    return {
        "msgtype": "markdown",
        "markdown": {
            "title": "{} {} 发布通知".format(brand.display_name, platform),
            "text": markdown,
        },
        "at": {
            "atMobiles": [tester.mobile for tester in brand.testers],
            "isAtAll": False,
        },
    }


def _safe_markdown_text(value: str, fallback: str, limit: int = 160) -> str:
    """压平不可信字段，避免 Jenkins 参数改变通知 Markdown 结构。"""
    text = " ".join(str(value or "").split())[:limit]
    text = re.sub(r"https?://\S+", "_link_", text, flags=re.IGNORECASE)
    for character in ("`", "#", "[", "]", "(", ")", "<", ">", "|"):
        text = text.replace(character, "_")
    return text or fallback


def _safe_build_url(value: str) -> str:
    parsed = urlsplit((value or "").strip())
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def failure_reason_for_stage(stage: str) -> str:
    return FAILURE_STAGE_SUMMARIES.get(
        (stage or "").strip(),
        "流水线执行失败，请打开 Jenkins 构建日志查看具体错误",
    )


def build_failure_markdown(
    job: str,
    build_number: str,
    result: str,
    stage: str,
    platform: str,
    brand: str,
    git_ref: str,
    build_url: str,
    work_item: Optional[WorkItemRef] = None,
    ui_engine: str = "",
) -> str:
    normalized_result = (result or "FAILURE").strip().upper()
    title = "Jenkins 构建不稳定" if normalized_result == "UNSTABLE" else "Jenkins 构建失败"
    safe_job = _safe_markdown_text(job, "unknown-job")
    safe_number = _safe_markdown_text(build_number, "unknown", limit=32)
    safe_stage = _safe_markdown_text(stage, "Pipeline")
    safe_platform = _safe_markdown_text(platform, "unknown", limit=40)
    safe_brand = _safe_markdown_text(brand, "不适用", limit=80)
    safe_ref = _safe_markdown_text(git_ref, "unknown")
    safe_url = _safe_build_url(build_url)
    reason = failure_reason_for_stage(stage)
    build_label = "{} #{}".format(safe_job, safe_number)
    build_value = "[{}]({})".format(build_label, safe_url) if safe_url else build_label
    lines = [
        "### ⚠️ {}".format(title),
        "",
        "**任务：** {}".format(safe_job),
        "",
        "**构建：** {}".format(build_value),
        "",
        "**结果：** {}".format(normalized_result),
        "",
        "**失败阶段：** {}".format(safe_stage),
        "",
        "**原因摘要：** {}".format(reason),
        "",
        "**平台 / 品牌：** {} / {}".format(safe_platform, safe_brand),
        "",
        "**Git 引用：** {}".format(safe_ref),
        "",
    ]
    if ui_engine in ("cmp", "kuikly"):
        lines.extend(["**UI 引擎：** {}".format("Kuikly" if ui_engine == "kuikly" else "CMP"), ""])
    if work_item is not None:
        lines.extend(
            [
                "**关联禅道：** [{}]({})".format(
                    work_item.display_name,
                    work_item.url,
                ),
                "",
            ]
        )
    lines.append("> 通知仅包含脱敏摘要；详细错误请通过 Jenkins 构建链接查看。")
    return "\n".join(lines)


def build_failure_payload(markdown: str, result: str) -> Dict[str, Any]:
    title = "Jenkins 构建不稳定" if result.upper() == "UNSTABLE" else "Jenkins 构建失败"
    return {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": markdown},
        "at": {"atMobiles": [], "isAtAll": False},
    }


def build_health_markdown(
    status: str,
    summary: str,
    jenkins_url: str,
    previous_status: str = "",
) -> str:
    normalized = status.strip().lower()
    titles = {
        "healthy": "Jenkins 已恢复",
        "degraded": "Jenkins 状态异常",
        "down": "Jenkins 无法访问",
    }
    title = titles.get(normalized, "Jenkins 健康检查异常")
    safe_summary = _safe_markdown_text(summary, "未提供状态摘要", limit=500)
    safe_url = _safe_build_url(jenkins_url)
    lines = [
        "### {}".format(title),
        "",
        "**当前状态：** {}".format(normalized.upper() or "UNKNOWN"),
        "",
        "**摘要：** {}".format(safe_summary),
        "",
    ]
    if previous_status:
        lines.extend(
            ["**状态变化：** {} → {}".format(previous_status.upper(), normalized.upper()), ""]
        )
    if safe_url:
        lines.extend(["[打开 Jenkins]({})".format(safe_url), ""])
    lines.append("> 该通知由 Jenkins 外部监控发送，不依赖 Controller 流水线。")
    return "\n".join(lines)


def build_health_payload(markdown: str, status: str) -> Dict[str, Any]:
    title = "Jenkins 已恢复" if status.lower() == "healthy" else "Jenkins 健康告警"
    return {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": markdown},
        "at": {"atMobiles": [], "isAtAll": False},
    }


def _post_payload(webhook: str, secret: str, payload: Mapping[str, Any], timeout: float) -> None:
    timestamp_ms = int(time.time() * 1000)
    url = signed_webhook(webhook, secret, timestamp_ms)
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
    except HTTPError as error:
        raise DingTalkError("钉钉 Webhook 请求失败：HTTP {}".format(error.code)) from error
    except (URLError, OSError, HTTPException) as error:
        raise DingTalkError("钉钉 Webhook 网络请求失败：{}".format(type(error).__name__)) from error
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DingTalkError("钉钉 Webhook 返回了无效 JSON") from error
    if not isinstance(result, dict):
        raise DingTalkError("钉钉 Webhook 返回了无效数据结构")
    error_code = result.get("errcode")
    if type(error_code) is not int:
        raise DingTalkError("钉钉 Webhook 返回了无效状态码")
    if error_code != 0:
        raise DingTalkRejectedError("钉钉通知失败：errcode {}".format(error_code))


def send_release_notification(
    release: ArtifactInfo,
    brand: BrandConfig,
    changelog: str,
    environment: str,
    branch: str,
    commit: str,
    builder: str,
    webhook: str,
    secret: str,
    timeout: float = 15.0,
    retry_delays: Sequence[float] = DELIVERY_RETRY_DELAYS,
    platform: str = "Android",
    distribution: str = "pgyer",
    work_item: Optional[WorkItemRef] = None,
    ui_engine: str = "cmp",
    artifact_sha256: str = "",
) -> str:
    markdown = build_markdown(
        release,
        brand,
        changelog,
        environment,
        branch,
        commit,
        builder,
        platform=platform,
        distribution=distribution,
        work_item=work_item,
        ui_engine=ui_engine,
    )
    payload = build_payload(markdown, brand, platform=platform)
    signed_webhook(webhook, secret, int(time.time() * 1000))
    if not isinstance(artifact_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256):
        raise DingTalkError("发布通知产物摘要无效")
    identity = {"operation": "release-notify", "brand": brand.key, "platform": platform,
                "ui_engine": ui_engine, "environment": environment, "commit": commit,
                "branch": branch, "build_key": release.build_key, "distribution": distribution,
                "artifact_sha256": artifact_sha256,
                "package_name": release.package_name, "version": release.version,
                "version_code": release.version_code,
                "channel_sha256": hashlib.sha256(webhook.encode()).hexdigest()}
    scope_fields = ("operation", "platform", "distribution", "channel_sha256", "build_key")
    if distribution in ("app-store-connect", "testflight"):
        scope_fields += ("package_name", "version", "version_code")
    scope = {key: identity[key] for key in scope_fields}
    namespace, legacy = delivery_namespace('dingtalk')
    with delivery_operation(identity, scope=scope, namespace=namespace, legacy_compatible=legacy) as (state, save):
        if state.get("status") == "sent":
            return markdown
        if state.get("status") == "pending":
            raise DingTalkError("上次钉钉发布通知结果未知；须人工查回，禁止重复发送")
        if state.get("status") != "rejected" and set(state) != {"version", "identity"}:
            raise DingTalkError("钉钉发布通知回执状态无效；禁止重复发送")
        save(status="pending")
        try:
            _send_with_retry("钉钉发布通知", webhook, secret, payload, timeout, retry_delays)
        except DingTalkRejectedError:
            save(status="rejected")
            raise
        save(status="sent")
    return markdown


def send_failure_notification(
    job: str,
    build_number: str,
    result: str,
    stage: str,
    platform: str,
    brand: str,
    git_ref: str,
    build_url: str,
    webhook: str,
    secret: str,
    work_item: Optional[WorkItemRef] = None,
    timeout: float = 15.0,
    retry_delays: Sequence[float] = DELIVERY_RETRY_DELAYS,
    ui_engine: str = "",
) -> str:
    markdown = build_failure_markdown(
        job=job,
        build_number=build_number,
        result=result,
        stage=stage,
        platform=platform,
        brand=brand,
        git_ref=git_ref,
        build_url=build_url,
        work_item=work_item,
        ui_engine=ui_engine,
    )
    payload = build_failure_payload(markdown, result)
    _send_with_retry("钉钉失败告警", webhook, secret, payload, timeout, retry_delays)
    return markdown


def send_health_notification(
    status: str,
    summary: str,
    jenkins_url: str,
    webhook: str,
    secret: str,
    previous_status: str = "",
    timeout: float = 15.0,
    retry_delays: Sequence[float] = DELIVERY_RETRY_DELAYS,
) -> str:
    markdown = build_health_markdown(status, summary, jenkins_url, previous_status)
    payload = build_health_payload(markdown, status)
    _send_with_retry("Jenkins 健康告警", webhook, secret, payload, timeout, retry_delays)
    return markdown


def _send_with_retry(
    operation: str,
    webhook: str,
    secret: str,
    payload: Mapping[str, Any],
    timeout: float,
    retry_delays: Sequence[float],
) -> None:
    for attempt in range(len(retry_delays) + 1):
        try:
            _post_payload(webhook, secret, payload, timeout)
            print("{}发送成功，attempt={}".format(operation, attempt + 1))
            return
        except DingTalkRejectedError as error:
            if attempt >= len(retry_delays):
                raise DingTalkRejectedError("{}在重试后仍失败：{}".format(operation, error)) from error
            delay = retry_delays[attempt]
            print("{}发送失败，{:.0f} 秒后重试（{}/{}）。".format(
                operation, delay, attempt + 1, len(retry_delays)
            ))
            time.sleep(delay)
