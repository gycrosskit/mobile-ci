#!/usr/bin/env python3
"""按已确认的品牌配置接入 iOS 测试签名；默认只读 plan，apply 才创建缺失资源。"""

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from project import SOURCE_ROOT

REPO_ROOT = SOURCE_ROOT
API_ROOT = "https://api.appstoreconnect.apple.com/v1/"
PROFILE_TYPES = {"pgyer": "IOS_APP_ADHOC", "testflight": "IOS_APP_STORE"}


class SigningError(RuntimeError):
    """可安全输出的签名接入错误。"""


class UnknownWrite(SigningError):
    """请求可能已被 Apple 接受，必须先查回结果。"""


def openssl(*args, data=None):
    command = ["openssl", *map(str, args)]
    result = subprocess.run(command, input=data,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    error = result.stderr.lower()
    if (result.returncode and args[0] == "pkcs12" and "-in" in args
            and "-export" not in args and "-legacy" not in args
            and b"unsupported" in error and re.search(rb"\brc2(?:[-_][a-z0-9]+)*\b", error)
            and not any(value in error for value in (b"invalid password", b"mac verify", b"bad decrypt", b"decode error"))):
        version = subprocess.run(["openssl", "version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if version.returncode == 0 and re.match(rb"OpenSSL 3\.", version.stdout):
            # 仅本次旧 RC2 P12 读取加载 provider；已有材料保持字节不变，后续身份校验照常执行。
            result = subprocess.run([*command, "-legacy"], input=data,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # OpenSSL stderr 可能包含路径或输入材料，不直接传入报告。
        raise SigningError("OpenSSL 校验失败（{}）；检查文件、密码和工具兼容性".format(args[0]))
    return result.stdout


def restricted(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise SigningError("秘密文件不存在或是符号链接")
    mode = path.stat()
    if mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) & 0o077:
        raise SigningError("秘密文件须属于当前用户且权限为 0600 或更严")
    return path.read_bytes()


def save(path, data):
    """同目录原子替换并 fsync；中断后保留旧值或完整新值。"""
    path = Path(path)
    if path.is_symlink():
        raise SigningError("拒绝覆盖符号链接")
    fd, temporary = tempfile.mkstemp(prefix=".signing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def public_key(key):
    return openssl("pkey", "-pubout", "-outform", "DER", data=key)


def certificate_info(content):
    pem = openssl("x509", "-inform", "DER", "-outform", "PEM", data=content)
    subject = openssl("x509", "-noout", "-subject", "-nameopt", "RFC2253", data=pem).decode()
    subject = re.sub(r"^subject=\s*", "", subject.strip())
    team = re.search(r"(?:^|[, ])OU=([A-Z0-9]{10})(?:,|$)", subject)
    if not team:
        raise SigningError("证书缺少明确 Developer Team OU")
    dates = openssl("x509", "-noout", "-dates", data=pem).decode().splitlines()
    starts = datetime.strptime(dates[0].split("=", 1)[1], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    if starts > datetime.now(timezone.utc):
        raise SigningError("证书尚未生效；停止，不自动替换")
    expires = dates[1].split("=", 1)[1]
    expiry = datetime.strptime(expires, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    pub = openssl("x509", "-pubkey", "-noout", data=pem)
    return {"pem": pem, "publicKey": openssl("pkey", "-pubin", "-outform", "DER", data=pub),
            "teamId": team.group(1), "expires": expiry, "der": content}


def p12_identity(p12, password):
    if not password.strip():
        raise SigningError("P12 密码不能为空")
    cert = openssl("pkcs12", "-in", p12, "-clcerts", "-nokeys", "-passin", "stdin", data=password)
    key = openssl("pkcs12", "-in", p12, "-nocerts", "-nodes", "-passin", "stdin", data=password)
    if cert.count(b"-----BEGIN CERTIFICATE-----") != 1 or key.count(b"PRIVATE KEY-----") != 2:
        raise SigningError("P12 必须包含唯一证书和私钥")
    info = certificate_info(openssl("x509", "-outform", "DER", data=cert))
    if public_key(key) != info["publicKey"]:
        raise SigningError("P12 私钥与证书不匹配")
    return key, info


def load_brand(config, brand, channels):
    root = json.loads(Path(config).read_text(encoding="utf-8"))
    if not re.fullmatch(r"[a-z][a-z0-9_]*", brand) or brand not in root:
        raise SigningError("未知品牌")
    ios = root[brand].get("ios", {})
    signing = ios.get("signing", {}).get("test", {})
    team_key = signing.get("team")
    team = root.get("_iosTeams", {}).get(team_key, {})
    names = signing.get("profileNames", {})
    if not re.fullmatch(r"[A-Z0-9]{10}", str(team.get("teamId", ""))):
        raise SigningError("品牌尚未确认测试 Team；先在 ci/brands.json 登记，禁止猜测")
    path = team.get("repositorySigningPath", "")
    if not re.fullmatch(r"signing/ios/[a-z][a-z0-9_-]*", path):
        raise SigningError("Team repositorySigningPath 未确认")
    if not ios.get("bundleId") or any(not names.get(channel) for channel in channels):
        raise SigningError("品牌 Bundle ID 或所选渠道 Profile 名称未确认")
    return {"brand": brand, "bundleId": ios["bundleId"], "team": team_key,
            "teamId": team["teamId"], "signingPath": path,
            "profileNames": {channel: names[channel] for channel in channels},
            "enabled": signing.get("enabled") is True}


def b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def jwt_signature(der):
    # OpenSSL 输出 ASN.1 ECDSA；JWT 要求两个固定 32 字节整数。
    if len(der) < 8 or der[0] != 0x30 or der[1] != len(der) - 2:
        raise SigningError("API Key 必须为 ES256 P-256 密钥")
    offset, values = 2, []
    for _ in range(2):
        if offset + 2 > len(der) or der[offset] != 2:
            raise SigningError("无效 ES256 签名")
        size = der[offset + 1]
        chunk = der[offset + 2:offset + 2 + size]
        if not size or len(chunk) != size:
            raise SigningError("无效 ES256 签名")
        value = int.from_bytes(chunk, "big")
        if value.bit_length() > 256:
            raise SigningError("API Key 必须为 ES256 P-256 密钥")
        values.append(value.to_bytes(32, "big"))
        offset += 2 + size
    if offset != len(der):
        raise SigningError("无效 ES256 签名")
    return b"".join(values)


class AppleAPI:
    def __init__(self, metadata=None):
        values = json.loads(restricted(metadata)) if metadata else {}
        self.key_path = Path(os.environ.get("ASC_API_KEY_PATH") or values.get("keyPath", ""))
        if metadata and not os.environ.get("ASC_API_KEY_PATH") and not self.key_path.is_absolute():
            self.key_path = Path(metadata).parent / self.key_path
        self.key_id = os.environ.get("ASC_API_KEY_ID") or values.get("keyId")
        self.issuer_id = os.environ.get("ASC_ISSUER_ID") or values.get("issuerId")
        self.team_id = os.environ.get("ASC_TEAM_ID") or values.get("teamId")
        if not self.key_id or not self.issuer_id:
            raise SigningError("缺少 ASC_API_KEY_ID / ASC_ISSUER_ID（或受限 API 配置）")
        restricted(self.key_path)
        key_type = openssl("pkey", "-in", self.key_path, "-text", "-noout").decode()
        if "prime256v1" not in key_type:
            raise SigningError("API Key 必须为 ES256 P-256 密钥")

    def request(self, path, body=None, method=None):
        method = method or ("POST" if body is not None else "GET")
        if method not in ("GET", "POST", "DELETE") or (method == "DELETE" and body is not None):
            raise SigningError("不支持的 Apple 请求方法")
        writing = method in ("POST", "DELETE")
        url = path if path.startswith("https://") else API_ROOT + path.lstrip("/")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.appstoreconnect.apple.com" or not parsed.path.startswith("/v1/"):
            raise SigningError("拒绝向非 Apple API 地址发送凭据")
        now = int(time.time())
        payload = b".".join([b64url(json.dumps({"alg": "ES256", "kid": self.key_id, "typ": "JWT"}).encode()),
                             b64url(json.dumps({"iss": self.issuer_id, "iat": now, "exp": now + 600,
                                                "aud": "appstoreconnect-v1"}).encode())])
        signature = openssl("dgst", "-sha256", "-sign", self.key_path, data=payload)
        token = (payload + b"." + b64url(jwt_signature(signature))).decode()
        request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                         headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=45) as response:
                return {} if method == "DELETE" and response.status == 204 else json.load(response)
        except urllib.error.HTTPError as error:
            if writing and error.code >= 500:
                raise UnknownWrite("Apple 写请求返回服务器错误；结果未知") from None
            raise SigningError("Apple API HTTP {}；未输出原始响应".format(error.code)) from None
        except (OSError, ValueError) as error:
            if writing:
                raise UnknownWrite("Apple 写请求中断或响应无法解析；结果未知") from None
            raise SigningError("Apple 只读查询失败；未输出原始响应") from None
        except SigningError:
            if writing:
                raise UnknownWrite("Apple 写请求出现意外重定向；结果未知") from None
            raise

    def all(self, path):
        result, visited = [], set()
        while path:
            if path in visited:
                raise SigningError("Apple 分页链接循环")
            visited.add(path)
            page = self.request(path)
            data = page["data"]
            result.extend(data if isinstance(data, list) else [data])
            path = page.get("links", {}).get("next")
        return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SigningError("Apple API 意外重定向；停止以保护凭据")


def remote_certificate(row):
    return certificate_info(base64.b64decode(row["attributes"]["certificateContent"], validate=True))


def expiry_warning(expires, warn_days, label):
    remaining = (expires - datetime.now(timezone.utc)).total_seconds() / 86400
    if remaining <= 0:
        raise SigningError(label + " 已过期；必须人工处理，不自动续签")
    return [label + " 将在 {} 天内过期，不自动续签".format(warn_days)] if remaining <= warn_days else []


def profile_check(api, profile, bundle_id, cert_id, device_ids, profile_type, warn_days):
    attrs = profile["attributes"]
    if attrs["profileState"] != "ACTIVE" or attrs["profileType"] != profile_type:
        raise SigningError("同名 Profile 状态或类型冲突；不自动替换/吊销")
    expires = datetime.fromisoformat(attrs["expirationDate"].replace("Z", "+00:00"))
    warnings = expiry_warning(expires, warn_days, "Profile")
    base = "profiles/{}/relationships/".format(urllib.parse.quote(profile["id"], safe=""))
    bundles = {row["id"] for row in api.all(base + "bundleId")}
    certs = {row["id"] for row in api.all(base + "certificates")}
    devices = {row["id"] for row in api.all(base + "devices")}
    if bundles != {bundle_id} or (cert_id is not None and certs != {cert_id}) or devices != device_ids:
        raise SigningError("同名 Profile 的 Bundle、证书或设备集合冲突；不自动替换/吊销")
    if cert_id is None:
        raise SigningError("同名 Profile 已存在但没有对应本地签名私钥；先恢复原 P12")
    return warnings


def create_once(api, path, body, marker, recover):
    """先记录意图；即使 POST 已成功但进程中断，重跑也只查询。"""
    found = recover()
    if found is not None:
        marker.unlink(missing_ok=True)
        return found
    if marker.exists():
        raise SigningError("上次 Apple 写入结果未知，重新查询仍未找到；须人工确认，不盲目重试")
    save(marker, json.dumps({"resource": path, "attemptedAt": datetime.now(timezone.utc).isoformat()}).encode())
    try:
        response = api.request(path, body)
        row = response.get("data")
        if not isinstance(row, dict) or not row.get("id"):
            raise UnknownWrite("Apple 写响应缺少资源标识")
    except UnknownWrite:
        row = recover()
        if row is None:
            raise SigningError("Apple 写入结果未知，查询仍未找到；保留私钥和标记，重跑仅查询") from None
    except SigningError:
        marker.unlink(missing_ok=True)
        raise
    # 这里不能删标记：调用方校验和持久化成功之后才能完成这次写入。
    return row


def setup(config, api, output, state, apply=False, warn_days=30,
          p12_source=None, password_source=None):
    output, state = Path(output), Path(state)
    target_p12, target_password = output / "distribution.p12", output / "distribution-p12.password"
    p12 = Path(p12_source) if p12_source else target_p12
    password_path = Path(password_source) if password_source else target_password
    if bool(p12_source) != bool(password_source):
        raise SigningError("已有签名材料输入必须同时指定 P12 和密码文件")
    if p12_source and (not p12.is_file() or not password_path.is_file()):
        raise SigningError("指定的已有签名材料不存在；停止，不能创建替代证书")
    if any(path.is_symlink() for path in (p12, password_path, target_p12, target_password)):
        raise SigningError("签名材料不能是符号链接")
    if p12_source:
        for source, target in ((p12, target_p12), (password_path, target_password)):
            if source != target and target.exists() and restricted(source) != restricted(target):
                raise SigningError("输入签名材料与输出目录冲突；不覆盖已有材料")
    key_path = state / "distribution.key"
    key, local_cert = None, None
    if p12.exists() and not password_path.exists():
        raise SigningError("已有 P12 缺少密码；停止，不能创建替代证书")
    if password_path.exists() and not p12.exists() and not key_path.is_file():
        raise SigningError("已有密码缺少 P12 和恢复私钥；停止，不能创建替代证书")
    if password_path.exists():
        password = restricted(password_path)
        if not password.strip():
            raise SigningError("P12 密码不能为空")
    else:
        password = None
    warnings = []
    if p12.exists():
        restricted(p12)
        key, local_cert = p12_identity(p12, password)
        if local_cert["teamId"] != config["teamId"]:
            raise SigningError("已有 P12 Developer Team 不匹配")
        warnings += expiry_warning(local_cert["expires"], warn_days, "证书")
    elif key_path.exists():
        key = restricted(key_path)
        public_key(key)
    if key_path.exists() and key is not None and public_key(restricted(key_path)) != public_key(key):
        raise SigningError("恢复私钥与已有 P12 不匹配；停止")
    if key is None and (state / "certificate-write.json").exists():
        raise SigningError("上次证书写入结果未知且原私钥/P12 已缺失；先恢复原签名材料，不能生成新私钥")

    certificates = api.all("certificates?limit=200")
    distribution = [row for row in certificates if row["attributes"]["certificateType"] in ("DISTRIBUTION", "IOS_DISTRIBUTION")]
    infos = [(row, remote_certificate(row)) for row in distribution]
    if any(info["teamId"] != config["teamId"] for _, info in infos) or (getattr(api, "team_id", None) and api.team_id != config["teamId"]):
        raise SigningError("API Key 返回的证书 Team 与所选品牌不匹配")
    if not infos and getattr(api, "team_id", None) != config["teamId"]:
        raise SigningError("Team 无分发证书可核对；须明确提供 ASC_TEAM_ID 或 API 配置 teamId")
    matches = [(row, info) for row, info in infos if key is not None and info["publicKey"] == public_key(key)]
    if len(matches) > 1:
        raise SigningError("多个 Apple 证书匹配同一私钥；须人工确认")
    cert = matches[0][0] if matches else None
    if local_cert and (not matches or matches[0][1]["der"] != local_cert["der"]):
        raise SigningError("已有 P12 证书未在所选 Team 找到（可能已吊销）；不自动新建")
    if matches:
        warnings += expiry_warning(matches[0][1]["expires"], warn_days, "证书")

    bundles = api.all("bundleIds?" + urllib.parse.urlencode({"filter[identifier]": config["bundleId"], "limit": 200}))
    bundles = [row for row in bundles if row["attributes"]["identifier"] == config["bundleId"] and row["attributes"].get("platform") in ("IOS", "UNIVERSAL")]
    if len(bundles) != 1:
        raise SigningError("所选 Team 未找到唯一已登记 iOS Bundle ID；不自动注册")
    bundle_id = bundles[0]["id"]
    devices = api.all("devices?limit=200") if "pgyer" in config["profileNames"] else []
    device_ids = {row["id"] for row in devices if row["attributes"]["platform"] == "IOS" and row["attributes"]["status"] == "ENABLED"}
    if "pgyer" in config["profileNames"] and not device_ids:
        raise SigningError("没有已登记且 ENABLED 的 iOS 设备；另走设备管理流程")
    profiles = api.all("profiles?limit=200")
    selected = {}
    for channel, name in config["profileNames"].items():
        found = [row for row in profiles if row["attributes"]["name"] == name]
        if len(found) > 1:
            raise SigningError("存在多个同名 Profile；须人工确认")
        profile = found[0] if found else None
        if profile:
            warnings += profile_check(api, profile, bundle_id, cert["id"] if cert else None,
                                      device_ids if channel == "pgyer" else set(), PROFILE_TYPES[channel], warn_days)
        selected[channel] = profile
    report = {"brand": config["brand"], "teamId": config["teamId"], "bundleId": config["bundleId"],
              "mode": "apply" if apply else "plan", "publishingEnabled": config["enabled"],
              "certificate": "reuse" if cert else ("created" if apply else "create-needed"),
              "certificateExpires": matches[0][1]["expires"].isoformat() if matches else None,
              "p12Verified": local_cert is not None,
              "iosDeviceCount": len(device_ids), "profiles": [], "warnings": sorted(set(warnings))}
    missing_profiles = {channel for channel, profile in selected.items() if profile is None}
    if apply:
        if key is None:
            key = openssl("genrsa", "2048")
            save(key_path, key)
        def recover_certificate():
            found = [row for row in api.all("certificates?limit=200")
                     if row["attributes"]["certificateType"] == "DISTRIBUTION" and remote_certificate(row)["publicKey"] == public_key(key)]
            if len(found) > 1:
                raise SigningError("多个 Apple 证书匹配同一私钥；须人工确认")
            return found[0] if found else None
        if cert is None:
            csr = openssl("req", "-new", "-key", "/dev/stdin", "-subj", "/CN=sxmqlive-test-signing/OU=" + config["teamId"], data=key).decode()
            cert = create_once(api, "certificates", {"data": {"type": "certificates", "attributes": {
                "certificateType": "DISTRIBUTION", "csrContent": csr}}}, state / "certificate-write.json", recover_certificate)
        info = remote_certificate(cert)
        if info["teamId"] != config["teamId"] or info["publicKey"] != public_key(key):
            raise SigningError("Apple 返回的证书 Team 或公钥不匹配；保留私钥待人工核对")
        report["warnings"] += expiry_warning(info["expires"], warn_days, "证书")
        report["certificateExpires"] = info["expires"].isoformat()
        if not p12.exists():
            if password is None:
                password = (secrets.token_urlsafe(32) + "\n").encode()
                save(target_password, password)
            with tempfile.TemporaryDirectory(prefix=".p12-", dir=state) as temporary:
                temporary = Path(temporary)
                save(temporary / "key.pem", key)
                save(temporary / "cert.pem", info["pem"])
                packed = openssl("pkcs12", "-export", "-inkey", temporary / "key.pem", "-in", temporary / "cert.pem",
                                 "-keypbe", "AES-256-CBC", "-certpbe", "AES-256-CBC", "-macalg", "sha256",
                                 "-passout", "stdin", data=password)
                save(temporary / "candidate.p12", packed)
                _, checked = p12_identity(temporary / "candidate.p12", password)
                if checked["der"] != info["der"]:
                    raise SigningError("生成 P12 校验失败")
                save(p12, packed)
        _, checked = p12_identity(p12, password)
        if checked["der"] != info["der"]:
            raise SigningError("持久化 P12 与 Apple 证书不匹配")
        if p12_source and p12 != target_p12:
            if not target_password.exists():
                save(target_password, password)
            if not target_p12.exists():
                save(target_p12, restricted(p12))
        key_path.unlink(missing_ok=True)
        (state / "certificate-write.json").unlink(missing_ok=True)
        report["p12Verified"] = True
        for channel, profile in selected.items():
            name = config["profileNames"][channel]
            marker = state / ("profile-" + hashlib.sha256(name.encode()).hexdigest() + "-write.json")
            if profile is None:
                def recover_profile():
                    found = [row for row in api.all("profiles?limit=200") if row["attributes"]["name"] == name]
                    if len(found) > 1:
                        raise SigningError("存在多个同名 Profile；须人工确认")
                    return found[0] if found else None
                relationships = {"bundleId": {"data": {"type": "bundleIds", "id": bundle_id}},
                                 "certificates": {"data": [{"type": "certificates", "id": cert["id"]}]}}
                if channel == "pgyer":
                    relationships["devices"] = {"data": [{"type": "devices", "id": value} for value in sorted(device_ids)]}
                profile = create_once(api, "profiles", {"data": {"type": "profiles", "attributes": {
                    "name": name, "profileType": PROFILE_TYPES[channel]}, "relationships": relationships}}, marker, recover_profile)
            report["warnings"] += profile_check(api, profile, bundle_id, cert["id"], device_ids if channel == "pgyer" else set(), PROFILE_TYPES[channel], warn_days)
            save(state / (config["brand"] + "-" + channel + ".mobileprovision"), base64.b64decode(profile["attributes"]["profileContent"], validate=True))
            marker.unlink(missing_ok=True)
            selected[channel] = profile
    for channel, profile in selected.items():
        report["profiles"].append({"channel": channel, "name": config["profileNames"][channel],
                                   "action": ("created" if apply else "create-needed") if channel in missing_profiles else "reuse",
                                   "expires": profile["attributes"]["expirationDate"] if profile else None})
    report["warnings"] = sorted(set(report["warnings"]))
    if apply:
        save(state / (config["brand"] + "-summary.json"), json.dumps(report, ensure_ascii=False, indent=2).encode())
    return report


@contextlib.contextmanager
def locked_directories(output, state):
    for directory in (output, state):
        if directory.is_symlink():
            raise SigningError("签名目录不能是符号链接")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.stat().st_uid != os.getuid():
            raise SigningError("签名目录须属于当前用户")
        directory.chmod(0o700)
    lock = state / ".setup.lock"
    if lock.is_symlink():
        raise SigningError("锁文件不能是符号链接")
    with open(lock, "a+b") as handle:
        os.chmod(lock, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SigningError("同一 Team 的签名接入正在执行") from None
        yield


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("plan", "apply"), default="plan")
    parser.add_argument("--brand", required=True)
    parser.add_argument("--channels", default="pgyer", help="pgyer、testflight 或逗号分隔的两者")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "ci/brands.json")
    parser.add_argument("--api-key-config", type=Path, help="0600 JSON：keyPath/keyId/issuerId，可选 teamId")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--p12-path", type=Path, help="只读复用已有 P12，须同时指定 --password-file")
    parser.add_argument("--password-file", type=Path, help="已有 P12 的 0600 密码文件")
    parser.add_argument("--warn-days", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        channels = list(dict.fromkeys(args.channels.split(",")))
        if not channels or any(channel not in PROFILE_TYPES for channel in channels) or args.warn_days < 0:
            raise SigningError("渠道或预警天数无效")
        if not shutil.which("openssl"):
            raise SigningError("需系统 OpenSSL；不需要 cryptography 或新增 Python 依赖")
        config = load_brand(args.config, args.brand, channels)
        output = args.output_dir or REPO_ROOT / config["signingPath"]
        state = args.state_dir or REPO_ROOT / "build/ios-signing-setup" / config["team"]
        api = AppleAPI(args.api_key_config)
        if args.command == "apply":
            with locked_directories(output, state):
                report = setup(config, api, output, state, True, args.warn_days, args.p12_path, args.password_file)
        else:
            report = setup(config, api, output, state, False, args.warn_days, args.p12_path, args.password_file)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except SigningError as error:
        print("iOS 签名接入失败：{}".format(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print("iOS 签名接入失败：文件或 API 数据无效；未输出原始内容", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
