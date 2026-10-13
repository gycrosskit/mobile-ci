#!/usr/bin/env python3
"""使用蒲公英 API 2.0 上传 APK、IPA 或 HAP 并等待发布完成。"""

import http.client
import json
import mimetypes
import socket
import ssl
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

import sys
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delivery.delivery import ArtifactInfo, artifact_sha256, delivery_operation, delivery_namespace, pgyer_build_key_matches, write_json_object
from delivery.delivery import delivery_urlopen as urlopen


GET_TOKEN_URL = "https://www.pgyer.com/apiv2/app/getCOSToken"
BUILD_INFO_URL = "https://www.pgyer.com/apiv2/app/buildInfo"
HARMONY_CERT_URL = "https://upload.pgyer.com/apiv2/app/uploadHarmonyCert"
DOWNLOAD_PAGE_BASE_URL = "https://www.pgyer.com/"
PUBLISHING_CODE = 1247
PUBLISH_FAILED_CODE = 1216


class PgyerError(RuntimeError):
    """蒲公英上传或发布失败，消息中不得包含密钥与签名。"""


def _api_code(payload: Mapping[str, Any], operation: str) -> int:
    try:
        return int(payload.get("code", -1))
    except (TypeError, ValueError) as error:
        raise PgyerError("{}返回了无效状态码".format(operation)) from error


def _decode_json(body: bytes, operation: str) -> Mapping[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PgyerError("{}返回了无效 JSON".format(operation)) from error
    if not isinstance(value, dict):
        raise PgyerError("{}返回的数据结构不正确".format(operation))
    return value


def _request_json(
    request: Request,
    operation: str,
    timeout: float,
) -> Mapping[str, Any]:
    try:
        with urlopen(request, timeout=timeout) as response:
            return _decode_json(response.read(), operation)
    except HTTPError as error:
        try:
            error.close()
        except (OSError, http.client.HTTPException):
            pass
        raise PgyerError("{}失败：HTTP {}".format(operation, error.code)) from error
    except (OSError, http.client.HTTPException) as error:
        raise PgyerError("{}网络请求失败：{}".format(operation, type(error).__name__)) from error


def _post_form_json(
    url: str,
    fields: Mapping[str, str],
    operation: str,
    timeout: float,
) -> Mapping[str, Any]:
    body = urlencode(fields).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    return _request_json(request, operation, timeout)


def _get_json(
    url: str,
    fields: Mapping[str, str],
    operation: str,
    timeout: float,
) -> Mapping[str, Any]:
    request = Request("{}?{}".format(url, urlencode(fields)), method="GET")
    return _request_json(request, operation, timeout)


def get_cos_token(
    api_key: str,
    update_description: str,
    install_password: Optional[str],
    channel_shortcut: Optional[str],
    timeout: float,
    build_type: str = "android",
) -> Tuple[str, str, Mapping[str, str]]:
    if build_type not in ("android", "ios", "harmonyos"):
        raise PgyerError("不支持的蒲公英构建类型：{}".format(build_type))
    fields = {
        "_api_key": api_key,
        "buildType": build_type,
        "buildUpdateDescription": update_description,
        "buildInstallType": "2" if install_password else "1",
    }
    if install_password:
        fields["buildPassword"] = install_password
    if channel_shortcut:
        fields["buildChannelShortcut"] = channel_shortcut

    payload = _post_form_json(GET_TOKEN_URL, fields, "获取蒲公英上传凭证", timeout)
    code = _api_code(payload, "获取蒲公英上传凭证")
    if code != 0:
        raise PgyerError("获取蒲公英上传凭证失败：code {}".format(code))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise PgyerError("上传凭证缺少 data")
    params = data.get("params")
    if not isinstance(params, dict):
        raise PgyerError("上传凭证缺少 params")

    endpoint = str(data.get("endpoint") or "")
    build_key = str(data.get("key") or params.get("key") or "")
    safe_params = {
        "key": str(params.get("key") or build_key),
        "signature": str(params.get("signature") or ""),
        "x-cos-security-token": str(params.get("x-cos-security-token") or ""),
    }
    if not endpoint or not build_key or not all(safe_params.values()):
        raise PgyerError("蒲公英上传凭证字段不完整")
    return endpoint, build_key, safe_params


def _multipart_parts(
    fields: Mapping[str, str],
    file_path: Path,
    boundary: str,
) -> Tuple[bytes, bytes]:
    chunks = []
    for name, value in fields.items():
        chunks.append(
            "--{0}\r\nContent-Disposition: form-data; name=\"{1}\"\r\n\r\n{2}\r\n".format(
                boundary, name, value
            ).encode("utf-8")
        )
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    chunks.append(
        (
            "--{0}\r\n"
            "Content-Disposition: form-data; name=\"file\"; filename=\"{1}\"\r\n"
            "Content-Type: {2}\r\n\r\n"
        ).format(boundary, file_path.name.replace('"', ""), mime_type).encode("utf-8")
    )
    prefix = b"".join(chunks)
    suffix = "\r\n--{}--\r\n".format(boundary).encode("ascii")
    return prefix, suffix


def _post_multipart(
    endpoint: str,
    params: Mapping[str, str],
    artifact_path: Path,
    timeout: float,
) -> Tuple[int, bytes]:
    parsed = urlsplit(endpoint)
    # 安装包和 COS 临时签名都属于敏感发布材料，不允许服务端响应把上传链路降级为明文 HTTP。
    if parsed.scheme != "https" or not parsed.hostname:
        raise PgyerError("蒲公英返回的上传地址必须是有效的 HTTPS 地址")

    boundary = "----CodexCI{}".format(uuid.uuid4().hex)
    prefix, suffix = _multipart_parts(params, artifact_path, boundary)
    content_length = len(prefix) + artifact_path.stat().st_size + len(suffix)
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=timeout)
    target = parsed.path or "/"
    if parsed.query:
        target = "{}?{}".format(target, parsed.query)
    try:
        connection.putrequest("POST", target)
        connection.putheader("Content-Type", "multipart/form-data; boundary={}".format(boundary))
        connection.putheader("Content-Length", str(content_length))
        connection.endheaders()
        connection.send(prefix)
        with artifact_path.open("rb") as artifact_file:
            while True:
                chunk = artifact_file.read(1024 * 1024)
                if not chunk:
                    break
                connection.send(chunk)
        connection.send(suffix)
        response = connection.getresponse()
        return response.status, response.read()
    except (OSError, http.client.HTTPException, socket.timeout, ssl.SSLError) as error:
        raise PgyerError("上传安装包网络请求失败：{}".format(type(error).__name__)) from error
    finally:
        connection.close()


def upload_file(endpoint, params, artifact_path, timeout):
    status, _ = _post_multipart(endpoint, {**params, "x-cos-meta-file-name": artifact_path.name}, artifact_path, timeout)
    if status != 204:
        raise PgyerError("上传安装包失败：HTTP {}".format(status))


def upload_harmony_certificate(api_key: str, build_key: str, p12: Path, password: str, timeout: float = 30.0) -> None:
    if not api_key or not build_key or not password or not p12.is_file() or p12.suffix != '.p12':
        raise PgyerError('鸿蒙安装清单签名需要 API Key、buildKey、P12 和密码')
    sha256 = artifact_sha256(p12)
    identity = {'operation': 'pgyer-certificate', 'build_key': build_key}
    namespace, legacy = delivery_namespace('pgyer')
    with delivery_operation(identity, namespace=namespace, legacy_compatible=legacy) as (state, save):
        if state.get('phase') is None and set(state) != {'version', 'identity'}:
            raise PgyerError('鸿蒙证书上传状态无效；未再次上传 P12')
        if state.get('phase') is not None and state.get('certificate_sha256') != sha256:
            raise PgyerError('本次 P12 与已记录证书上传不一致；未再次上传 P12')
        if state.get('phase') == 'completed':
            return
        if state.get('phase') not in (None, 'rejected'):
            raise PgyerError('鸿蒙证书上传结果未知；必须人工核对，未再次上传 P12')
        if artifact_sha256(p12) != sha256:
            raise PgyerError('P12 在上传前改变；未上传 P12')
        save(phase='pending', certificate_sha256=sha256)
        # P12 仅送固定官方入口；错误响应可能反射密码，不输出厂商原始消息。
        status, body = _post_multipart(HARMONY_CERT_URL, {'_api_key': api_key, 'buildKey': build_key, 'password': password}, p12, timeout)
        if artifact_sha256(p12) != sha256:
            raise PgyerError('P12 在上传过程中改变；保留未知结果，未确认分发成功')
        if status != 200:
            raise PgyerError('鸿蒙证书上传失败：HTTP {}'.format(status))
        payload = _decode_json(body, '鸿蒙证书上传')
        if type(payload.get('code')) not in (int, str):
            raise PgyerError('鸿蒙证书上传返回了无效状态码')
        code = _api_code(payload, '鸿蒙证书上传')
        if code != 0:
            save(phase='rejected')
            raise PgyerError('鸿蒙证书上传失败：code {}'.format(code))
        save(phase='completed')


def _download_url(shortcut: str) -> str:
    shortcut = shortcut.strip()
    if not shortcut:
        return ""
    if shortcut.startswith(("https://", "http://")):
        return shortcut
    return DOWNLOAD_PAGE_BASE_URL + shortcut.lstrip("/")


def release_info_from_api(data: Mapping[str, Any]) -> ArtifactInfo:
    release = ArtifactInfo(
        name=str(data.get("buildName") or ""),
        version=str(data.get("buildVersion") or ""),
        version_code=str(data.get("buildVersionNo") or ""),
        package_name=str(data.get("buildIdentifier") or ""),
        build_key=str(data.get("buildKey") or ""),
        download_url=_download_url(str(data.get("buildShortcutUrl") or "")),
        qr_code_url=str(data.get("buildQRCodeURL") or ""),
    )
    missing = [
        field
        for field, value in (
            ("buildKey", release.build_key),
            ("buildName", release.name),
            ("buildVersion", release.version),
            ("buildVersionNo", release.version_code),
            ("buildIdentifier", release.package_name),
            ("buildShortcutUrl", release.download_url),
        )
        if not value
    ]
    if missing:
        raise PgyerError("发布结果缺少字段：{}".format(", ".join(missing)))
    return release


def wait_for_release(
    api_key: str,
    build_key: str,
    timeout: float = 120.0,
    poll_interval: float = 3.0,
    request_timeout: float = 30.0,
) -> ArtifactInfo:
    deadline = time.monotonic() + timeout
    while True:
        try:
            payload = _get_json(
                BUILD_INFO_URL,
                {"_api_key": api_key, "buildKey": build_key},
                "查询蒲公英发布状态",
                request_timeout,
            )
        except PgyerError as error:
            if time.monotonic() + poll_interval > deadline:
                raise PgyerError("等待蒲公英发布超时，最后一次查询失败：{}".format(error)) from error
            print("查询蒲公英发布状态暂时失败，{} 秒后继续查询。".format(int(poll_interval)))
            time.sleep(poll_interval)
            continue
        code = _api_code(payload, "查询蒲公英发布状态")
        if code == 0:
            data = payload.get("data")
            if not isinstance(data, dict):
                raise PgyerError("发布结果缺少 data")
            return release_info_from_api(data)
        if code == PUBLISH_FAILED_CODE:
            raise PgyerError("蒲公英发布失败：code {}".format(code))
        if code != PUBLISHING_CODE:
            raise PgyerError("查询蒲公英发布状态失败：code {}".format(code))
        if time.monotonic() + poll_interval > deadline:
            raise PgyerError("等待蒲公英发布超时（{} 秒）".format(int(timeout)))
        print("蒲公英仍在发布，{} 秒后继续查询。".format(int(poll_interval)))
        time.sleep(poll_interval)


def upload_artifact(
    artifact_path: str,
    api_key: str,
    update_description: str,
    platform: str,
    install_password: Optional[str] = None,
    channel_shortcut: Optional[str] = None,
    request_timeout: float = 30.0,
    publish_timeout: float = 120.0,
    poll_interval: float = 3.0,
    delivery_identity: Optional[Mapping[str, Any]] = None,
) -> ArtifactInfo:
    """上传安装包；任何日志都只输出非敏感的路径、状态与 buildKey。"""
    suffix_by_platform = {"android": ".apk", "ios": ".ipa", "harmonyos": ".hap"}
    expected_suffix = suffix_by_platform.get(platform)
    if expected_suffix is None:
        raise PgyerError("不支持的蒲公英平台：{}".format(platform))
    path = Path(artifact_path).resolve()
    if not path.is_file() or path.suffix.lower() != expected_suffix:
        raise PgyerError(
            "{} 文件不存在或扩展名不正确：{}".format(expected_suffix.upper(), path)
        )
    if not api_key:
        raise PgyerError("缺少 PGYER_API_KEY")

    if set(delivery_identity or {}) - {'platform', 'brand', 'ui_engine', 'environment', 'commit', 'branch'}:
        raise PgyerError('蒲公英交付身份包含不支持的字段')
    sha256 = artifact_sha256(path)
    identity = {'brand': '', **dict(delivery_identity or {}), 'operation': 'pgyer-upload', 'platform': platform,
                'artifact_sha256': sha256, 'channel_shortcut': channel_shortcut or ''}
    scope = {key: identity[key] for key in ('operation', 'platform', 'brand')}
    namespace, legacy = delivery_namespace('pgyer')
    with delivery_operation(identity, scope=scope, namespace=namespace, legacy_compatible=legacy) as (state, save):
        if artifact_sha256(path) != sha256:
            raise PgyerError('安装包在上传前改变；未执行上传')
        phase = state.get('phase')
        if phase is None and set(state) != {'version', 'identity'}:
            raise PgyerError('蒲公英上传状态无效；未重复上传')
        if phase == 'complete':
            receipt = state.get('receipt')
            if (not isinstance(receipt, dict) or not all(receipt.get(field) for field in (
                    'name', 'version', 'version_code', 'package_name', 'build_key', 'download_url'))
                    or receipt.get('build_key') != state.get('build_key')
                    or ('upload_key' in state and not pgyer_build_key_matches(
                        state['upload_key'], receipt.get('build_key'), platform))):
                raise PgyerError('蒲公英持久分发结果不完整；未执行重复上传')
            return ArtifactInfo.from_dict(receipt)
        if phase is None:
            save(phase='pending')
            endpoint, build_key, params = get_cos_token(
                api_key, update_description, install_password, channel_shortcut, request_timeout, platform,
            )
            save(phase='token', build_key=build_key)
            print("已获取上传凭证，buildKey：{}".format(build_key))
            if artifact_sha256(path) != sha256:
                raise PgyerError('安装包在获取凭证后改变；未执行上传')
            save(phase='upload-pending')
            upload_file(endpoint, params, path, request_timeout)
            if artifact_sha256(path) != sha256:
                raise PgyerError('安装包在上传过程中改变；保留未知结果，未确认分发成功')
            save(phase='uploaded')
            print("安装包上传完成，等待蒲公英发布。")
        elif phase not in ('upload-pending', 'uploaded') or not state.get('build_key'):
            raise PgyerError('蒲公英上传结果未知或缺少可恢复凭证；必须人工核对，未重复上传')
        release = wait_for_release(
            api_key=api_key,
            build_key=state['build_key'],
            timeout=publish_timeout,
            poll_interval=poll_interval,
            request_timeout=request_timeout,
        )
        if identity.get('ui_engine'):
            release = release._replace(ui_engine=identity['ui_engine'])
        upload_key = state['build_key']
        if not pgyer_build_key_matches(upload_key, release.build_key, platform):
            raise PgyerError('蒲公英发布结果 buildKey 与本次上传不一致；保留原恢复状态')
        if artifact_sha256(path) != sha256:
            raise PgyerError('安装包在查询发布结果时改变；未确认分发成功')
        save(phase='complete', build_key=release.build_key, upload_key=upload_key, receipt=release.to_dict())
        return release


def write_release_info(release: ArtifactInfo, output: Path) -> None:
    write_json_object(output, release.to_dict())
