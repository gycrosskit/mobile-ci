#!/usr/bin/env python3
"""HarmonyOS 指定设备发布签名接入；plan 只读，apply 才创建，devices 默认只读。"""

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from project import SOURCE_ROOT

REPO_ROOT = SOURCE_ROOT
API_ROOT = "https://connect-api.cloud.huawei.com"
AUDIENCE = "https://oauth-login.cloud.huawei.com/oauth2/v3/token"
CERT_PATH = "/api/publish/v3/cert"
DEVICE_PATH = "/api/publish/v2/device"
PROFILE_PATH = "/api/publish/v3/provision"


class ProvisioningError(RuntimeError):
    """只包含可安全输出的错误信息。"""


class RejectedWrite(ProvisioningError):
    """明确 ret.code 非零；服务已返回业务拒绝，可撤销本次 pending。"""


class UnknownWrite(ProvisioningError):
    """请求可能已生效；先查询，禁止自动重复创建。"""


def openssl(*args, data=None):
    result = subprocess.run(["openssl", *map(str, args)], input=data,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise ProvisioningError("OpenSSL 操作失败（{}）；检查输入与工具兼容性".format(args[0]))
    return result.stdout


def restricted(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ProvisioningError("秘密文件不存在或是符号链接")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ProvisioningError("秘密文件必须属于当前用户且权限为 0600 或更严")
    return path.read_bytes()


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise ProvisioningError("受控目录不能是符号链接")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ProvisioningError("受控目录必须属于当前用户且权限为 0700 或更严")
    return path


def save(path, content):
    path = Path(path)
    if path.is_symlink():
        raise ProvisioningError("拒绝覆盖符号链接")
    fd, temporary = tempfile.mkstemp(prefix=".harmony-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(content):
    try:
        return json.loads(content)
    except (ValueError, UnicodeError):
        raise ProvisioningError("JSON 输入无效；未输出原始材料") from None


def load_brand(path, brand):
    try:
        root = read_json(Path(path).read_bytes())
    except OSError:
        raise ProvisioningError("品牌配置读取失败") from None
    if not isinstance(root, dict):
        raise ProvisioningError("品牌配置根节点必须为对象")
    value = root.get(brand, {})
    harmony = value.get("harmony", {}) if isinstance(value, dict) else {}
    if not isinstance(harmony, dict):
        raise ProvisioningError("HarmonyOS 品牌配置必须为对象")
    team_key = harmony.get("teamKey")
    teams = root.get("_harmonyTeams", {})
    team = teams.get(team_key, {}) if isinstance(teams, dict) and isinstance(team_key, str) else {}
    signing = harmony.get("signing", {})
    if not isinstance(signing, dict) or not isinstance(team, dict):
        raise ProvisioningError("HarmonyOS Team 或 signing 配置无效")
    required = {"brand": brand, "teamKey": team_key,
                "appId": harmony.get("appId"), "bundleName": harmony.get("bundleName"),
                "certName": signing.get("certName"), "profileName": signing.get("profileName"),
                "credentialNamespace": team.get("credentialNamespace")}
    if any(not isinstance(item, str) or not item.strip() for item in required.values()):
        raise ProvisioningError("先确认 HarmonyOS Team、appId、bundleName、签名名称和独立凭据命名空间")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", brand):
        raise ProvisioningError("品牌 ID 无效")
    required["enabled"] = signing.get("enabled") is True
    return required


def b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def jwt(key_path, key_id, sub_account, now=None):
    restricted(key_path)
    now = int(time.time()) if now is None else now
    message = b".".join(b64url(json.dumps(value, separators=(",", ":")).encode()) for value in (
        {"kid": key_id, "typ": "JWT", "alg": "PS256"},
        {"aud": AUDIENCE, "iss": sub_account, "iat": now, "exp": now + 600}))
    signature = openssl("dgst", "-sha256", "-sign", key_path,
                        "-sigopt", "rsa_padding_mode:pss", "-sigopt", "rsa_pss_saltlen:32", data=message)
    return (message + b"." + b64url(signature)).decode()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProvisioningError("API 意外重定向；停止以保护凭据")


class HuaweiAPI:
    def __init__(self):
        self.key_path = os.environ.get("HARMONY_SA_KEY_PATH")
        self.key_id = os.environ.get("HARMONY_SA_KEY_ID")
        self.sub_account = os.environ.get("HARMONY_SA_SUB_ACCOUNT")
        if not all((self.key_path, self.key_id, self.sub_account)):
            raise ProvisioningError("缺少 HARMONY_SA_KEY_PATH / HARMONY_SA_KEY_ID / HARMONY_SA_SUB_ACCOUNT")
        restricted(self.key_path)
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, method="GET", body=None, headers=None):
        if not path.startswith("/api/publish/") or "#" in path or ".." in path:
            raise ProvisioningError("拒绝向非 Huawei Publish API 地址发送凭据")
        token = jwt(self.key_path, self.key_id, self.sub_account)
        request = urllib.request.Request(API_ROOT + path, method=method,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"Authorization": "Bearer " + token,
                                                  "Content-Type": "application/json", **(headers or {})})
        writing = method in ("POST", "PUT") and not path.split("?", 1)[0].endswith("/list")
        try:
            with self.opener.open(request, timeout=45) as response:
                content = response.read(8 * 1024 * 1024 + 1)
            if len(content) > 8 * 1024 * 1024:
                raise ValueError("oversized response")
            result = read_json(content)
        except urllib.error.HTTPError as error:
            if writing and error.code >= 500:
                raise UnknownWrite("Huawei 写请求服务器错误；结果未知，请查回") from None
            raise ProvisioningError("Huawei API HTTP {}；未输出原始响应".format(error.code)) from None
        except (OSError, ValueError, ProvisioningError):
            if writing:
                raise UnknownWrite("Huawei 写请求中断或响应无效；结果未知，请查回") from None
            raise ProvisioningError("Huawei 只读请求失败；未输出原始响应") from None
        try:
            return check_response(result)
        except ProvisioningError:
            ret = result.get("ret") if isinstance(result, dict) else None
            if writing and (not isinstance(ret, dict) or type(ret.get("code")) is not int):
                raise UnknownWrite("Huawei 写响应 ret 结构未知；结果未知，请查回") from None
            raise


def read_devices(path):
    if not path:
        return []
    result = read_json(restricted(path))
    if not isinstance(result, list) or len(result) > 1000:
        raise ProvisioningError("设备输入必须为不超过 1000 项的 JSON 数组")
    devices = {}
    for row in result:
        if not isinstance(row, dict) or set(row) != {"deviceName", "udid", "deviceType"}:
            raise ProvisioningError("设备必须含且仅含 deviceName、udid、deviceType")
        if (not isinstance(row["deviceName"], str) or not row["deviceName"].strip() or len(row["deviceName"]) > 100
                or not isinstance(row["udid"], str) or not re.fullmatch(r"[A-Za-z0-9-]{16,64}", row["udid"])
                or type(row["deviceType"]) is not int or row["deviceType"] != 4):
            raise ProvisioningError("设备名称、UDID 或 HarmonyOS deviceType=4 无效；未输出设备材料")
        if row["udid"] in devices and devices[row["udid"]] != row:
            raise ProvisioningError("重复 UDID 的设备参数冲突；未输出设备材料")
        devices[row["udid"]] = row
    return list(devices.values())


def check_response(result):
    ret = result.get("ret") if isinstance(result, dict) else None
    if not isinstance(ret, dict) or type(ret.get("code")) is not int:
        raise ProvisioningError("Huawei API ret 响应结构未知；停止")
    if ret["code"] != 0:
        raise RejectedWrite("Huawei API 业务失败（code={}）；未输出原始响应".format(ret["code"]))
    return result


def certificate_rows(result):
    fields = [field for field in ("certInfo", "certList") if field in result]
    if len(fields) != 1 or not isinstance(result[fields[0]], list):
        raise ProvisioningError("证书列表须为 certInfo 或 certList 数组；未知结构停止")
    rows = result[fields[0]]
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not isinstance(row.get("certName"), str) or str(row.get("certType")) != "2"
                or not isinstance(row.get("certDownloadUrl"), str)):
            raise ProvisioningError("发布证书响应结构未知；停止")
    return rows


def list_certificates(api):
    return certificate_rows(api.request(CERT_PATH + "/list", "POST", {"certType": 2}))


def paged_rows(api, path, field, headers=None, optional_total=False):
    rows, seen, start = [], set(), 1
    total = None
    while True:
        result = api.request(path + "?" + urllib.parse.urlencode({"fromRecCount": start, "maxReqCount": 100}),
                             headers=headers)
        page = result.get(field)
        current_total = result.get("totalCount")
        # AGC 真正空设备池的首响应会省略 deviceList，仅确认总数为 int 0 时允许。
        if start == 1 and field == "deviceList" and field not in result and type(current_total) is int and current_total == 0:
            page = []
        if (not isinstance(page, list) or (current_total is None and (not optional_total or "totalCount" in result))
                or (current_total is not None and (type(current_total) is not int or current_total < 0))):
            raise ProvisioningError("分页响应 totalCount 或列表结构未知；停止")
        if total is not None and current_total is not None and total != current_total:
            raise ProvisioningError("分页期间资源总数变化；请重新只读查询")
        if current_total is not None:
            total = current_total
        if len(page) > 100:
            raise ProvisioningError("分页响应超过请求上限")
        for row in page:
            identity = row.get("id") if isinstance(row, dict) else None
            if not isinstance(identity, str) or not identity or identity in seen:
                raise ProvisioningError("分页缺少 ID 或发生重复；fromRecCount 语义须实测，停止避免遗漏")
            seen.add(identity)
            rows.append(row)
        if total is not None:
            if len(rows) == total:
                return rows
            if not page or len(rows) > total:
                raise ProvisioningError("分页数量与 totalCount 不一致；停止")
        elif len(page) < 100:
            return rows
        if start >= 1000:
            raise ProvisioningError("分页超过 1000 页安全上限；停止")
        # shortcut: 按官方 starting page 递增，若服务实际按记录偏移解释则重复 ID 检查阻断，实测后再调整。
        start += 1


def list_devices(api):
    rows = paged_rows(api, DEVICE_PATH + "/list", "deviceList")
    for row in rows:
        if (not isinstance(row.get("udid"), str) or not re.fullmatch(r"[A-Za-z0-9-]{16,64}", row["udid"])
                or str(row.get("deviceType")) not in ("1", "2", "4", "5", "8", "19")):
            raise ProvisioningError("设备列表结构未知；未输出原始响应")
    udids = [row["udid"] for row in rows]
    if len(udids) != len(set(udids)):
        raise ProvisioningError("远端设备 UDID 重复；停止")
    return rows


def list_profiles(api, config):
    return paged_rows(api, PROFILE_PATH + "/list", "provisionList", {"appId": config["appId"]}, optional_total=True)


def sync_devices(api, requested, apply=False, state=None):
    existing = list_devices(api)
    known = {row["udid"]: row for row in existing}
    for row in requested:
        if row["udid"] in known and str(known[row["udid"]]["deviceType"]) != "4":
            raise ProvisioningError("已有 UDID 的平台类型冲突；停止")
    missing = [row for row in requested if row["udid"] not in known]
    marker = state / "devices-pending.json" if state is not None else None
    if apply and marker is not None and marker.exists():
        previous = read_json(restricted(marker))
        pending = previous.get("deviceList") if isinstance(previous, dict) else None
        if (not isinstance(pending, list) or any(not isinstance(item, dict) or not isinstance(item.get("udid"), str)
                                                for item in pending)):
            raise ProvisioningError("设备恢复状态无效；停止")
        if any(item["udid"] not in known or str(known[item["udid"]]["deviceType"]) != "4" for item in pending):
            raise ProvisioningError("设备注册结果未知且查询未恢复；保留成功项，禁止重复 POST")
        marker.unlink()
    if missing and apply:
        if marker is not None:
            if marker.exists():
                raise ProvisioningError("设备注册结果未知且查询未恢复；保留成功项，禁止重复 POST")
            pending_write(marker, {"deviceList": missing})
        partial_failure = False
        try:
            result = api.request(DEVICE_PATH, "POST", {"deviceList": missing})
            succeeded, failed = result.get("successedCount"), result.get("failedCount")
            details = result.get("failedAddDeviceDetail", [] if failed == 0 else None)
            if (type(succeeded) is not int or type(failed) is not int or min(succeeded, failed) < 0
                    or succeeded + failed != len(missing) or not isinstance(details, list) or len(details) != failed):
                raise UnknownWrite("设备 POST 计数结构无效；查询恢复，禁止直接重试")
            failed_udids = []
            for detail in details:
                info = detail.get("addDeviceInfo") if isinstance(detail, dict) else None
                ret = detail.get("result") if isinstance(detail, dict) else None
                if (not isinstance(info, dict) or not isinstance(info.get("udid"), str)
                        or info["udid"] not in {row["udid"] for row in missing}
                        or not isinstance(ret, dict) or type(ret.get("code")) is not int or ret["code"] == 0
                        or info["udid"] in failed_udids):
                    raise UnknownWrite("设备失败明细无法对应本次 UDID；查询恢复，禁止直接重试")
                failed_udids.append(info["udid"])
            partial_failure = failed > 0
        except RejectedWrite:
            if marker is not None and marker.exists():
                marker.unlink()
            raise
        except UnknownWrite:
            pass
        existing = list_devices(api)
        known = {row["udid"]: row for row in existing}
        if partial_failure or any(row["udid"] not in known or str(known[row["udid"]]["deviceType"]) != "4"
                                  for row in requested):
            raise ProvisioningError("设备未完整注册；已重新查询，保留成功项，禁止盲目重试")
    if apply and marker is not None and marker.exists():
        marker.unlink()
    selected = [known[row["udid"]] for row in requested if row["udid"] in known]
    return selected, len(missing)


def device_union(existing, additional):
    if not isinstance(existing, list) or not isinstance(additional, list):
        raise ProvisioningError("Profile 设备集合必须为数组")
    identities = []
    for row in existing + additional:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise ProvisioningError("Profile 必须使用 AGC device id；禁止把 UDID 当作 ID")
        if row["id"] not in identities:
            identities.append(row["id"])
    if len(identities) > 4000:
        raise ProvisioningError("Profile 设备数量超过 4000")
    return identities


def download(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ProvisioningError("下载 URL 结构无效；未输出地址") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or port not in (None, 443)):
        raise ProvisioningError("下载 URL 必须为不含身份信息的 HTTPS 地址")
    # 下载地址只来自已认证 API；不携带 Bearer，也不跟随重定向。
    try:
        with urllib.request.build_opener(NoRedirect()).open(url, timeout=45) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if not data or len(data) > 8 * 1024 * 1024:
            raise ValueError("invalid length")
        return data
    except (OSError, ValueError, ProvisioningError):
        raise ProvisioningError("签名材料下载失败；未输出临时 URL 或响应") from None


def certificate_pem(content):
    fmt = "PEM" if b"-----BEGIN CERTIFICATE-----" in content else "DER"
    pem = openssl("x509", "-inform", fmt, "-outform", "PEM", data=content)
    openssl("x509", "-checkend", "0", "-noout", data=pem)
    start = openssl("x509", "-startdate", "-noout", data=pem).decode().strip().split("=", 1)[-1]
    try:
        not_before = ssl.cert_time_to_seconds(start)
    except ValueError:
        raise ProvisioningError("证书生效时间无法解析") from None
    if not_before > time.time():
        raise ProvisioningError("发布证书尚未生效")
    return pem


def certificate_chain(content):
    """保留官方下载中的完整链，避免 x509 只保留第一张证书。"""
    if b"-----BEGIN CERTIFICATE-----" not in content:
        try:
            return certificate_pem(content)
        except ProvisioningError:
            content = openssl("pkcs7", "-inform", "DER", "-print_certs", data=content)
    blocks = re.findall(br"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", content, re.DOTALL)
    if not blocks:
        raise ProvisioningError("下载材料没有可解析的发布证书")
    normalized = [certificate_pem(block) for block in blocks]
    fingerprints = [hashlib.sha256(openssl("x509", "-outform", "DER", data=block)).digest() for block in normalized]
    if len(set(fingerprints)) != len(fingerprints):
        raise ProvisioningError("发布证书链包含重复证书；停止")
    return b"".join(normalized)


def public_key(key):
    return openssl("pkey", "-pubout", "-outform", "DER", data=key)


def certificate_public_key(pem):
    value = openssl("x509", "-pubkey", "-noout", data=pem)
    return openssl("pkey", "-pubin", "-outform", "DER", data=value)


def matching_certificate(api, config, expected):
    matches = []
    for row in list_certificates(api):
        if row["certName"] != config["certName"]:
            continue
        pem = certificate_chain(download(row["certDownloadUrl"]))
        blocks = re.findall(br"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem, re.DOTALL)
        leaves = [block for block in blocks if certificate_public_key(block) == expected]
        if len(leaves) > 1:
            raise ProvisioningError("下载证书链有多个证书匹配同一私钥；停止")
        if leaves:
            ordered = [leaves[0]] + [block for block in blocks if block != leaves[0]]
            matches.append((row, b"\n".join(ordered)))
    if len(matches) > 1:
        raise ProvisioningError("多个证书匹配同一私钥；须人工确认，禁止按名称任选")
    return matches[0] if matches else None


def pending_write(path, body):
    save(path, json.dumps(body, sort_keys=True).encode())


def ensure_certificate(api, config, state, apply):
    key_path = state / "release.key"
    key = restricted(key_path) if key_path.exists() else None
    expected = public_key(key) if key else None
    found = matching_certificate(api, config, expected) if key else None
    if found:
        if apply and (state / "certificate-pending.json").exists():
            previous = read_json(restricted(state / "certificate-pending.json"))
            if (not isinstance(previous, dict) or previous.get("certName") != config["certName"]
                    or previous.get("publicKeySha256") != hashlib.sha256(expected).hexdigest()):
                raise ProvisioningError("证书恢复状态与当前私钥或名称冲突；停止")
            (state / "certificate-pending.json").unlink()
        return found, "reuse"
    if not apply:
        return None, "create-needed"
    if not key:
        # P-256 已通过指定设备扫码安装；已有私钥仍复用，切换证书需受控迁移。
        key = openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-pkeyopt", "ec_param_enc:named_curve")
        save(key_path, key)
        expected = public_key(key)
    marker = state / "certificate-pending.json"
    if marker.exists():
        raise ProvisioningError("已有结果未知的证书请求且查询未恢复；保留私钥，禁止重复创建")
    csr = openssl("req", "-new", "-newhdr", "-sha256", "-key", key_path,
                  "-subj", "/C=CN/O=HUAWEI/OU=HUAWEI IDE/CN=release").decode()
    body = {"csr": csr, "certName": config["certName"], "certType": 2}
    pending_write(marker, {"certName": config["certName"], "publicKeySha256": hashlib.sha256(expected).hexdigest()})
    try:
        api.request(CERT_PATH, "POST", body)
    except RejectedWrite:
        marker.unlink()
        raise
    except UnknownWrite:
        pass
    found = matching_certificate(api, config, expected)
    if not found:
        raise ProvisioningError("证书写入后未查回匹配公钥；保留私钥和恢复状态，禁止盲目重试")
    marker.unlink()
    return found, "created"


def validate_profile(data, config, certificate, devices):
    # 校验 CMS 签名完整性；Huawei 签发链信任仍由官方签包工具验收。
    decoded = openssl("cms", "-verify", "-inform", "DER", "-noverify", data=data)
    profile = read_json(decoded)
    if not isinstance(profile, dict) or profile.get("type") != "release":
        raise ProvisioningError("指定设备 Profile 必须为实际签名的 release 内容")
    info, validity = profile.get("bundle-info"), profile.get("validity")
    debug = profile.get("debug-info")
    if not isinstance(info, dict) or info.get("bundle-name") != config["bundleName"]:
        raise ProvisioningError("Profile bundle-name 与品牌不匹配")
    cert = info.get("distribution-certificate")
    if not isinstance(cert, str):
        raise ProvisioningError("Profile 缺少发布证书")
    embedded = certificate_pem(cert.encode())
    if openssl("x509", "-outform", "DER", data=embedded) != openssl("x509", "-outform", "DER", data=certificate):
        raise ProvisioningError("Profile 发布证书与已核实发布证书不匹配")
    now = int(time.time())
    if (not isinstance(validity, dict) or type(validity.get("not-before")) is not int
            or type(validity.get("not-after")) is not int
            or not validity["not-before"] <= now < validity["not-after"]):
        raise ProvisioningError("Profile 有效期无效或已过期")
    if not isinstance(debug, dict) or debug.get("device-id-type") != "udid":
        raise ProvisioningError("Profile 缺少已核实的 debug-info UDID 结构；需检查实际 p7b 契约")
    identifiers = debug.get("device-ids")
    if (not isinstance(devices, list) or any(not isinstance(row, dict) or not isinstance(row.get("udid"), str)
                                           for row in devices)):
        raise ProvisioningError("Profile 设备响应缺少明确 UDID；停止")
    expected = {row["udid"] for row in devices}
    if (not isinstance(identifiers, list) or any(not isinstance(item, str) for item in identifiers)
            or None in expected or set(identifiers) != expected or len(set(identifiers)) != len(identifiers)):
        raise ProvisioningError("Profile 实际 UDID 与完整设备集合不匹配；未输出设备材料")
    return profile


def selected_profile(api, config):
    rows = [row for row in list_profiles(api, config) if row.get("provisionName") == config["profileName"]]
    if len(rows) > 1:
        raise ProvisioningError("多个同名 Profile；须人工确认，禁止任选")
    if not rows:
        return None
    row = rows[0]
    if (str(row.get("provisionType")) != "6" or row.get("appId") != config["appId"]
            or str(row.get("tempFlag")) != "0" or row.get("certName") != config["certName"]
            or not isinstance(row.get("deviceList"), list)
            or not isinstance(row.get("provisionDownloadUrl"), str)):
        raise ProvisioningError("已有 Profile 类型、应用、证书或设备结构冲突；停止")
    identities = device_union(row["deviceList"], [])
    if len(identities) != len(row["deviceList"]):
        raise ProvisioningError("Profile 设备 AGC ID 重复；停止")
    registered = {device["id"]: device for device in list_devices(api)}
    resolved = []
    for device in row["deviceList"]:
        actual = registered.get(device["id"])
        masked = device.get("udid")
        if (actual is None or str(device.get("deviceType")) != str(actual["deviceType"])
                or not isinstance(masked, str) or len(masked) != len(actual["udid"])
                or any(char != "*" and char != actual["udid"][index] for index, char in enumerate(masked))):
            raise ProvisioningError("Profile 设备缺项、类型或 UDID 可见位置冲突；未输出设备材料")
        resolved.append(dict(actual))
    return {**row, "deviceList": resolved}


def ensure_profile(api, config, state, certificate, devices, apply=False):
    row = selected_profile(api, config)
    current = row["deviceList"] if row else []
    identities = device_union(current, devices)
    all_devices = {item["id"]: item for item in current + devices}
    target_devices = [all_devices[identity] for identity in identities]
    if not identities:
        raise ProvisioningError("指定设备发布 Profile 需要至少一个已注册 HarmonyOS 设备")
    namespace = hashlib.sha256((config["appId"] + "\0" + config["profileName"]).encode()).hexdigest()[:24]
    marker = state / ("profile-" + namespace + "-pending.json")
    needed = row is None or set(identities) != {item["id"] for item in current}
    if apply and marker.exists():
        previous = read_json(restricted(marker))
        request = previous.get("request") if isinstance(previous, dict) else None
        previous_ids = request.get("deviceIdList") if isinstance(request, dict) else None
        if (not isinstance(previous_ids, list) or any(not isinstance(identity, str) for identity in previous_ids)
                or row is None or set(previous_ids) != {item["id"] for item in current}):
            raise ProvisioningError("Profile 请求结果未知且查询未恢复完整原设备集合；禁止重复写入")
        validate_profile(download(row["provisionDownloadUrl"]), config, certificate[1], current)
        marker.unlink()
    if row:
        validate_profile(download(row["provisionDownloadUrl"]), config, certificate[1], current)
    if not apply:
        return None, "create-needed" if row is None else "update-needed" if needed else "reuse", target_devices
    if needed:
        if marker.exists():
            raise ProvisioningError("已有结果未知的 Profile 请求且查询未恢复；禁止重复写入")
        if row:
            # PUT 是完整集合替换；只发送设备 union，避免覆盖既有 ACL 配置。
            body = {"provisionId": row["id"], "deviceIdList": identities}
            method = "PUT"
        else:
            body = {"provisionName": config["profileName"], "provisionType": 6,
                    "certId": certificate[0]["id"], "appId": config["appId"], "deviceIdList": identities}
            method = "POST"
        pending_write(marker, {"method": method, "request": body})
        try:
            api.request(PROFILE_PATH, method, body)
        except RejectedWrite:
            marker.unlink()
            raise
        except UnknownWrite:
            pass
        row = selected_profile(api, config)
        if row is None or set(device_union(row["deviceList"], [])) != set(identities):
            raise ProvisioningError("Profile 写入后未查回完整设备集合；保留恢复状态，禁止盲目重试")
    content = download(row["provisionDownloadUrl"])
    validate_profile(content, config, certificate[1], target_devices)
    if marker.exists():
        marker.unlink()
    return content, "created" if not current and needed else "updated" if needed else "reuse", target_devices


def export_p12(key_path, certificate, password):
    if not password or not password.strip():
        raise ProvisioningError("缺少 HARMONY_P12_PASSWORD；禁止生成无密码 P12")
    with tempfile.TemporaryDirectory(prefix="harmony-p12-") as temporary:
        root = Path(temporary)
        chain = re.findall(br"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", certificate, re.DOTALL)
        if not chain:
            raise ProvisioningError("导出 P12 缺少发布证书")
        save(root / "release.pem", chain[0])
        extra = []
        if len(chain) > 1:
            save(root / "chain.pem", b"\n".join(chain[1:]))
            extra = ["-certfile", root / "chain.pem"]
        packed = openssl("pkcs12", "-export", "-inkey", key_path, "-in", root / "release.pem",
                         *extra, "-name", "release", "-keypbe", "AES-256-CBC", "-certpbe", "AES-256-CBC",
                         "-macalg", "sha256", "-passout", "stdin", data=password.encode())
        save(root / "release.p12", packed)
        exported_cert = openssl("pkcs12", "-in", root / "release.p12", "-clcerts", "-nokeys",
                                "-passin", "stdin", data=password.encode())
        exported_key = openssl("pkcs12", "-in", root / "release.p12", "-nocerts", "-nodes",
                               "-passin", "stdin", data=password.encode())
        if (certificate_public_key(certificate_pem(exported_cert)) != public_key(exported_key)
                or public_key(exported_key) != public_key(restricted(key_path))):
            raise ProvisioningError("导出 P12 私钥与证书不匹配")
    return packed


def stable_p12(state, certificate, password):
    if not password or not password.strip():
        raise ProvisioningError("缺少 HARMONY_P12_PASSWORD；禁止读取或导出无密码 P12")
    path = state / "release.p12"
    if not path.exists():
        packed = export_p12(state / "release.key", certificate, password)
        save(path, packed)
        return packed
    packed = restricted(path)
    cert = openssl("pkcs12", "-in", path, "-clcerts", "-nokeys", "-passin", "stdin", data=password.encode())
    all_certs = openssl("pkcs12", "-in", path, "-nokeys", "-passin", "stdin", data=password.encode())
    def fingerprints(value):
        return {hashlib.sha256(openssl("x509", "-outform", "DER", data=block)).digest()
                for block in re.findall(br"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", value, re.DOTALL)}
    if fingerprints(all_certs) != fingerprints(certificate):
        raise ProvisioningError("已有受控 P12 缺少或包含不同的官方证书链；禁止覆盖")
    key = openssl("pkcs12", "-in", path, "-nocerts", "-nodes", "-passin", "stdin", data=password.encode())
    if (openssl("x509", "-outform", "DER", data=certificate_pem(cert)) != openssl("x509", "-outform", "DER", data=certificate)
            or public_key(key) != public_key(restricted(state / "release.key"))):
        raise ProvisioningError("已有受控 P12 与当前发布身份冲突；禁止覆盖")
    return packed


def setup(config, api, state, output, requested, apply=False, devices_only=False):
    if devices_only:
        certificate = ensure_certificate(api, config, state, False)[0]
        if not certificate:
            raise ProvisioningError("设备同步需先完成首次签名接入，受控 state 中须保留匹配私钥")
    selected, missing = sync_devices(api, requested, apply, state)
    report = {"brand": config["brand"], "teamKey": config["teamKey"], "appId": config["appId"],
              "requestedDeviceCount": len(requested), "missingDeviceCount": missing,
              "applied": apply, "publishingEnabled": config["enabled"]}
    if not devices_only:
        certificate, report["certificate"] = ensure_certificate(api, config, state, apply)
    if certificate is None:
        report["profile"] = "blocked-certificate"
        return report
    content, report["profile"], profile_devices = ensure_profile(api, config, state, certificate, selected, apply)
    report["profileDeviceCount"] = len(profile_devices)
    if apply:
        packed = stable_p12(state, certificate[1], os.environ.get("HARMONY_P12_PASSWORD"))
        save(output / "release.p12", packed)
        save(output / "release.cer", openssl("x509", "-outform", "DER", data=certificate[1]))
        save(output / "designated-release.p7b", content)
        save(output / "devices.json", json.dumps(profile_devices, sort_keys=True).encode())
        report["certificateChainCount"] = certificate[1].count(b"-----BEGIN CERTIFICATE-----")
        report["p12Verified"] = True
        report["profileVerified"] = True
        report["profileSha256"] = hashlib.sha256(content).hexdigest()
    return report


@contextlib.contextmanager
def state_lock(path):
    lock = path / ".lock"
    if lock.is_symlink():
        raise ProvisioningError("受控 state 锁不能为符号链接")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise ProvisioningError("同一 state 正在执行另一签名任务；禁止并行写入") from None
    finally:
        os.close(fd)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply", "devices"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=REPO_ROOT / "ci/brands.json")
        command.add_argument("--brand", required=True)
        command.add_argument("--state-dir", type=Path, required=True)
        command.add_argument("--output-dir", type=Path)
        command.add_argument("--devices-file", type=Path, required=name == "devices")
        if name == "devices":
            command.add_argument("--apply", action="store_true")
    return parser


def main():
    args = build_parser().parse_args()
    try:
        config = load_brand(args.config, args.brand)
        requested = read_devices(args.devices_file)
        apply = args.command == "apply" or getattr(args, "apply", False)
        if apply and not args.output_dir:
            raise ProvisioningError("apply 必须指定 --output-dir 受控临时材料目录")
        if apply and not os.environ.get("HARMONY_P12_PASSWORD"):
            raise ProvisioningError("apply 缺少 HARMONY_P12_PASSWORD")
        api = HuaweiAPI()
        if apply:
            state, output = private_dir(args.state_dir), private_dir(args.output_dir)
            with state_lock(state):
                report = setup(config, api, state, output, requested, True, args.command == "devices")
        else:
            # plan 不创建目录、锁或密钥，也不注册设备。
            report = setup(config, api, args.state_dir, args.output_dir, requested, False, args.command == "devices")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (ProvisioningError, OSError):
        # OSError 可能含带私密材料的路径；只输出明确允许的领域错误。
        error = sys.exc_info()[1]
        print("HarmonyOS 签名接入失败：" + (str(error) if isinstance(error, ProvisioningError) else "受控文件操作失败"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
