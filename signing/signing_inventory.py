#!/usr/bin/env python3
"""集中签名台账；只读供应商，仅缓存经过 Profile 核对的已批准设备。"""

import argparse
import base64
import hashlib
import html
import json
import os
import plistlib
import re
import ssl
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from project import SOURCE_ROOT

from signing import ios_signing_setup as ios, harmony_provisioning as harmony, signing_devices, android_signing

ROOT = SOURCE_ROOT
SAFE_KEY = re.compile(r"[a-z][a-z0-9_-]*")
PUBLIC_TARGET = ("brand", "displayName", "platform", "team", "teamId", "companyName", "namespace",
                 "bundleId", "bundleName", "appId", "enabled", "channelEnabled", "profileNames",
                 "applicationId", "signingSource", "keyAlias", "keyOwner",
                 "credentialIds", "configurationStatus", "configurationNotes")


class ProfileMismatch(ValueError):
    """已完成查询后发现明确身份或设备冲突。"""


def targets(config, platform="all", brands="all"):
    root = json.loads(Path(config).read_text(encoding="utf-8"))
    if not isinstance(root, dict) or platform not in ("all", "android", "ios", "harmony"):
        raise ValueError("品牌配置或平台无效")
    available = [key for key, value in root.items() if not key.startswith("_") and isinstance(value, dict)]
    selected = available if brands == "all" else list(dict.fromkeys(value.strip() for value in brands.split(",")))
    if not selected or any(not SAFE_KEY.fullmatch(key) or key not in available for key in selected):
        raise ValueError("未知或无效品牌")
    rows = []
    for brand in selected:
        for kind in ("android", "ios", "harmony") if platform == "all" else (platform,):
            value = root[brand].get(kind, {})
            value = value if isinstance(value, dict) else {}
            signing = value.get("signing", {})
            signing = signing if isinstance(signing, dict) else {}
            notes, names, channels = [], {}, {}
            if kind == "android":
                credentials = signing.get("credentials", {})
                credentials = credentials if isinstance(credentials, dict) else {}
                keystore_id = credentials.get("keystore", "")
                group = re.fullmatch(r"(android-key-[a-z][a-z0-9-]*)-keystore", str(keystore_id))
                credentials_valid = bool(group and credentials.get("signingJson") == group[1] + "-signing-json")
                ready = (signing.get("source") == "jenkins" and credentials_valid
                         and isinstance(signing.get("keyAlias"), str) and bool(signing["keyAlias"].strip())
                         and re.fullmatch(r"[a-fA-F0-9]{64}", str(signing.get("certificateSha256", ""))) is not None
                         and bool(value.get("applicationId")))
                rows.append({"brand": brand, "displayName": str(root[brand].get("displayName") or brand),
                             "platform": kind, "team": "", "teamId": "", "companyName": value.get("companyName", ""),
                             "namespace": "", "applicationId": value.get("applicationId", ""),
                             "keyAlias": signing.get("keyAlias", ""), "keyOwner": signing.get("keyOwner", ""),
                             "signingSource": signing.get("source", ""), "enabled": ready,
                             "channelEnabled": {}, "profileNames": {},
                             "credentialIds": {"keystore": keystore_id, "signingConfig": credentials["signingJson"]} if credentials_valid else {},
                             "configurationStatus": "configured" if ready else "pending", "configurationNotes": notes})
                continue
            if kind == "ios":
                test, release = signing.get("test", {}), signing.get("release", {})
                test = test if isinstance(test, dict) else {}
                release = release if isinstance(release, dict) else {}
                team_key = test.get("team") or release.get("team")
                names.update(test.get("profileNames", {}) if isinstance(test.get("profileNames"), dict) else {})
                channels.update({channel: test.get("enabled") is True for channel in names})
                if release.get("team") and test.get("team") and release["team"] != test["team"]:
                    notes.append("正式渠道 Team 不同，待独立核对；本行只查询测试 Team")
                elif release.get("profileName"):
                    names["appstore"] = release["profileName"]
                    channels["appstore"] = release.get("enabled") is True
                for channel in (test, release):
                    if channel.get("blockReason"):
                        notes.append(str(channel["blockReason"]))
                team = root.get("_iosTeams", {}).get(team_key, {})
                suffixes = {"apiKey": "asc-api-key", "apiKeyId": "asc-key-id", "apiIssuerId": "asc-issuer-id",
                            "p12": "distribution-p12", "p12Password": "distribution-p12-password"}
                ready = bool(value.get("bundleId") and names and re.fullmatch(r"[A-Z0-9]{10}", str(team.get("teamId", ""))))
            else:
                team_key = value.get("teamKey")
                team = root.get("_harmonyTeams", {}).get(team_key, {})
                if signing.get("profileName"):
                    names["designated"] = signing["profileName"]
                    channels["designated"] = signing.get("enabled") is True
                suffixes = {"apiKey": "sa-key", "apiKeyId": "sa-key-id", "apiSubAccount": "sa-sub-account",
                            "p12": "release-p12", "p12Password": "release-p12-password",
                            "profile": brand + "-designated-release-profile", "devices": brand + "-devices-json"}
                ready = bool(value.get("bundleName") and value.get("appId") and signing.get("certName") and names)
            namespace = team.get("credentialNamespace", "")
            namespace_valid = re.fullmatch(r"[a-z][a-z0-9-]*", str(namespace)) is not None
            if namespace_valid and any(key != team_key and row.get("credentialNamespace") == namespace
                                       for key, row in root.get("_iosTeams" if kind == "ios" else "_harmonyTeams", {}).items()):
                namespace_valid = False
                notes.append("Team 凭据命名空间冲突")
            ready = ready and namespace_valid and SAFE_KEY.fullmatch(str(team_key)) is not None
            rows.append({"brand": brand, "displayName": str(root[brand].get("displayName") or brand),
                         "platform": kind, "team": team_key or "", "teamId": team.get("teamId", ""),
                         "companyName": team.get("companyName", ""), "namespace": namespace if namespace_valid else "",
                         "keyOwner": team.get("keyOwner", ""),
                         "signingSource": team.get("testSigningSource", "repository") if kind == "ios" else "jenkins",
                         "bundleId": value.get("bundleId", ""), "bundleName": value.get("bundleName", ""),
                         "appId": value.get("appId", ""), "enabled": any(channels.values()), "channelEnabled": channels,
                         "profileNames": {key: str(name) for key, name in names.items()},
                         "credentialIds": {key: namespace + "-" + suffix for key, suffix in suffixes.items()} if namespace_valid else {},
                         "configurationStatus": "configured" if ready else "pending", "configurationNotes": notes})
    return rows


class ReadOnlyApple(ios.AppleAPI):
    def request(self, path, body=None, method=None):
        if body is not None or method not in (None, "GET"):
            raise ValueError("台账禁止 Apple 写请求")
        return super().request(path, method="GET")


class ReadOnlyHuawei(harmony.HuaweiAPI):
    def request(self, path, method="GET", body=None, headers=None):
        if method != "GET" and not (method == "POST" and path == harmony.CERT_PATH + "/list" and body == {"certType": 2}):
            raise ValueError("台账禁止 Huawei 写请求")
        return super().request(path, method, body, headers)


def utc(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return datetime.fromtimestamp(value, timezone.utc) if type(value) is int else datetime.fromisoformat(value.replace("Z", "+00:00"))


def validity(starts, expires):
    now = datetime.now(timezone.utc)
    return "not-yet-valid" if utc(starts) > now else "expired" if utc(expires) <= now else "expiring" if (utc(expires) - now).total_seconds() <= 30 * 86400 else "valid"


def certificate(content):
    # 台账必须展示过期材料，不能复用发布入口的过期即抛错转换器。
    fmt = "PEM" if b"-----BEGIN CERTIFICATE-----" in content else "DER"
    pem = ios.openssl("x509", "-inform", fmt, "-outform", "PEM", data=content)
    der = ios.openssl("x509", "-outform", "DER", data=pem)
    details = ios.openssl("x509", "-noout", "-dates", "-subject", "-issuer", "-serial", "-nameopt", "RFC2253", data=pem).decode()
    fields = dict(line.split("=", 1) for line in details.splitlines() if "=" in line)
    starts, expires = [datetime.fromtimestamp(ssl.cert_time_to_seconds(fields[key]), timezone.utc) for key in ("notBefore", "notAfter")]
    subject = fields["subject"].strip()
    team = re.search(r"(?:^|,)OU=([A-Z0-9]{10})(?:,|$)", subject)
    return {"name": subject, "issuer": fields.get("issuer", "").strip(), "serial": fields.get("serial", ""), "sha256": hashlib.sha256(der).hexdigest(),
            "startsAt": starts.isoformat(), "expiresAt": expires.isoformat(), "status": validity(starts, expires),
            "teamId": team.group(1) if team else "", "_der": der, "_pem": pem}


def public_certificate(value):
    return {key: item for key, item in value.items() if not key.startswith("_")}


def cms(content):
    return ios.openssl("cms", "-verify", "-inform", "DER", "-noverify", data=content)


def mask(value):
    return value[:4] + "…" + value[-4:] if len(value) > 8 else "••••"


def material_certificate(platform, config, row, statuses):
    path = os.environ.get("IOS_DISTRIBUTION_P12" if platform == "ios" else "HARMONY_EXISTING_P12_PATH")
    password = os.environ.get("IOS_DISTRIBUTION_P12_PASSWORD" if platform == "ios" else "HARMONY_P12_PASSWORD")
    if any(statuses.get(row["credentialIds"].get(role)) == "wrong-type" for role in ("p12", "p12Password")):
        raise ValueError("绑定凭据类型错误")
    if platform == "ios" and bool(path) != bool(password):
        raise ValueError("绑定 P12/password 缺项")
    if platform == "ios" and not path:
        team = json.loads(Path(config).read_text())["_iosTeams"][row["team"]]
        if team.get("testSigningSource", "repository") != "repository":
            return None
        if any(statuses.get(row["credentialIds"].get(role)) == "configured" for role in ("p12", "p12Password")):
            raise ValueError("已有 Jenkins 签名凭据未完整绑定")
        relative = team.get("repositorySigningPath", "")
        if not re.fullmatch(r"signing/ios/[a-z][a-z0-9_-]*", relative):
            raise ValueError("仓库签名目录无效")
        source = ROOT / relative
        if any(value.is_symlink() for value in (ROOT, ROOT / "signing", ROOT / "signing/ios", source)):
            raise ValueError("仓库签名目录不能包含符号链接")
        p12, secret = source / "distribution.p12", source / "distribution-p12.password"
        if p12.is_symlink() or secret.is_symlink() or p12.exists() != secret.exists():
            raise ValueError("仓库 P12/password 缺项或符号链接")
        if not p12.exists():
            return None
        with tempfile.TemporaryDirectory(prefix="inventory-ios-") as directory:
            temporary = Path(directory)
            temporary.chmod(0o700)
            copies = []
            for origin in (p12, secret):
                target = temporary / origin.name
                with target.open("xb") as handle:
                    target.chmod(0o600)
                    handle.write(origin.read_bytes())
                copies.append(target)
            _, info = ios.p12_identity(copies[0], ios.restricted(copies[1]))
            result = certificate(info["der"])
            result["_source"] = "repository"
            return result
    if not path or not password:
        return None
    ios.restricted(path)
    cert = ios.openssl("pkcs12", "-in", path, "-clcerts", "-nokeys", "-passin", "stdin", data=password.encode())
    if cert.count(b"-----BEGIN CERTIFICATE-----") != 1:
        raise ValueError("P12 证书身份不唯一")
    key = ios.openssl("pkcs12", "-in", path, "-nocerts", "-nodes", "-passin", "stdin", data=password.encode())
    info = certificate(cert)
    pub = ios.openssl("x509", "-pubkey", "-noout", data=info["_pem"])
    if key.count(b"PRIVATE KEY-----") != 2 or ios.public_key(key) != ios.openssl("pkey", "-pubin", "-outform", "DER", data=pub):
        raise ValueError("P12 私钥和证书不匹配")
    info["_source"] = "Jenkins"
    return info


def _ios_inventory(row, local, report):
    api = ReadOnlyApple()
    certificates = api.all("certificates?limit=200")
    remote = {}
    for item in certificates:
        if item["attributes"].get("certificateType") not in ("DISTRIBUTION", "IOS_DISTRIBUTION"):
            continue
        info = certificate(base64.b64decode(item["attributes"]["certificateContent"], validate=True))
        info.update(id=item["id"], type=item["attributes"]["certificateType"], matchesP12=local is not None and local["_der"] == info["_der"])
        if info["teamId"] != row["teamId"]:
            raise ValueError("证书 Team 不一致")
        remote[item["id"]] = info
    report["certificates"] = [public_certificate(info) for info in remote.values()]
    devices = {item["id"]: item["attributes"] for item in api.all("devices?limit=200")}
    profiles = api.all("profiles?limit=200")
    bundles = api.all("bundleIds?" + ios.urllib.parse.urlencode({"filter[identifier]": row["bundleId"], "limit": 200}))
    bundle_ids = {item["id"] for item in bundles if item["attributes"].get("identifier") == row["bundleId"] and item["attributes"].get("platform") in ("IOS", "UNIVERSAL")}
    for channel, name in row["profileNames"].items():
        result = {"channel": channel, "name": name, "status": "missing", "devicePolicy": "指定设备" if channel == "pgyer" else "TestFlight / App Store 不限制 UDID"}
        report["profiles"].append(result)
        matches = [item for item in profiles if item["attributes"].get("name") == name]
        if len(matches) > 1:
            result["status"] = "conflict"
            continue
        if not matches:
            continue
        item, attrs = matches[0], matches[0]["attributes"]
        result.update(id=item["id"], type=attrs.get("profileType", ""), state=attrs.get("profileState", ""), expiresAt=attrs.get("expirationDate", ""), status="unknown")
        try:
            content = base64.b64decode(attrs["profileContent"], validate=True)
            profile = plistlib.loads(cms(content))
            starts, expires = profile["CreationDate"], profile["ExpirationDate"]
            result.update(startsAt=utc(starts).isoformat(), expiresAt=utc(expires).isoformat(), status=validity(starts, expires), cmsSignatureVerified=True, caTrustVerified=False)
            base = "profiles/" + ios.urllib.parse.quote(item["id"], safe="") + "/relationships/"
            related_bundles = {entry["id"] for entry in api.all(base + "bundleId")}
            related_certs = {entry["id"] for entry in api.all(base + "certificates")}
            related_devices = {entry["id"] for entry in api.all(base + "devices")}
            expected_type = "IOS_APP_ADHOC" if channel == "pgyer" else "IOS_APP_STORE"
            entitlements = profile.get("Entitlements", {})
            prefixes = profile.get("ApplicationIdentifierPrefix", [])
            if (len(bundle_ids) != 1 or related_bundles != bundle_ids or profile.get("Name") != name
                    or profile.get("TeamIdentifier") != [row["teamId"]] or attrs.get("profileType") != expected_type
                    or attrs.get("profileState") != "ACTIVE" or entitlements.get("com.apple.developer.team-identifier") != row["teamId"]
                    or entitlements.get("application-identifier") not in [prefix + "." + row["bundleId"] for prefix in prefixes]
                    or entitlements.get("get-task-allow", False) is not False or profile.get("ProvisionsAllDevices", False) is not False
                    or utc(expires) != utc(attrs["expirationDate"]) or len(related_certs) != 1):
                raise ProfileMismatch("Profile 身份或状态冲突")
            signer = remote.get(next(iter(related_certs)))
            if signer is None or signer["_der"] not in profile.get("DeveloperCertificates", []):
                raise ProfileMismatch("Profile 证书冲突")
            result["certificateIds"] = sorted(related_certs)
            full = [devices[identity] for identity in sorted(related_devices)]
            expected = {device["udid"].lower() for device in full}
            actual = profile.get("ProvisionedDevices", [])
            if channel == "pgyer":
                if (not full or any(device.get("platform") != "IOS" or device.get("status") != "ENABLED" for device in full)
                        or not isinstance(actual, list) or len(set(value.lower() for value in actual)) != len(actual)
                        or {value.lower() for value in actual} != expected):
                    raise ProfileMismatch("Profile 设备集合冲突")
            elif related_devices or "ProvisionedDevices" in profile or entitlements.get("beta-reports-active") is not True:
                raise ProfileMismatch("App Store Profile 设备类型冲突")
            result.update(deviceCount=len(full), identityVerified=True, materialReady=signer["matchesP12"])
            result["approvedForReuse"] = channel == "pgyer" and signer["matchesP12"] and signer["status"] in ("valid", "expiring") and result["status"] in ("valid", "expiring")
            if channel == "pgyer":
                record_devices(report, "ios", [{"name": device.get("name", ""), "udid": device["udid"], "deviceType": "IOS"} for device in full],
                               {"brand": row["brand"], "team": row["team"], "profile": name}, result["approvedForReuse"])
        except ProfileMismatch:
            result.update(status="mismatch", identityVerified=False, materialReady=False)
        except Exception:
            result.update(status="unknown", identityVerified=False, materialReady=False)
            report["errors"].append("Profile 查询或解析未完成；状态未知，未输出原始响应")


def _harmony_inventory(row, local, report, config):
    api = ReadOnlyHuawei()
    settings = harmony.load_brand(config, row["brand"])
    remote = []
    for item in harmony.list_certificates(api):
        if item["certName"] != settings["certName"]:
            continue
        raw = harmony.download(item["certDownloadUrl"])
        blocks = re.findall(br"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", raw, re.S)
        infos = [certificate(block) for block in blocks] if blocks else [certificate(raw)]
        matched = [info for info in infos if local is not None and local["_der"] == info["_der"]]
        info = matched[0] if len(matched) == 1 else infos[0]
        info.update(id=item["id"], type=str(item["certType"]), configuredName=item["certName"], matchesP12=len(matched) == 1, _chain=infos)
        remote.append(info)
    report["certificates"] = [public_certificate(info) for info in remote]
    devices = {item["id"]: item for item in harmony.list_devices(api)}
    profiles = [item for item in harmony.list_profiles(api, settings) if item.get("provisionName") == settings["profileName"]]
    result = {"channel": "designated", "name": settings["profileName"], "status": "conflict" if len(profiles) > 1 else "missing", "devicePolicy": "指定设备"}
    report["profiles"].append(result)
    if len(profiles) != 1:
        return
    item = profiles[0]
    result.update(id=item["id"], type=str(item.get("provisionType", "")), status="unknown")
    try:
        content = harmony.download(item["provisionDownloadUrl"])
        profile = json.loads(cms(content))
        starts, expires = profile["validity"]["not-before"], profile["validity"]["not-after"]
        if type(starts) is not int or type(expires) is not int:
            raise ValueError("Profile 日期结构错误")
        result.update(startsAt=utc(starts).isoformat(), expiresAt=utc(expires).isoformat(), status=validity(starts, expires), cmsSignatureVerified=True, caTrustVerified=False)
        info, debug = profile["bundle-info"], profile["debug-info"]
        embedded = certificate(info["distribution-certificate"].encode())
        if len(remote) == 1:
            matches = [value for value in remote[0]["_chain"] if value["_der"] == embedded["_der"]]
            if len(matches) == 1:
                previous = remote[0]
                remote[0] = {**matches[0], **{key: previous[key] for key in ("id", "type", "configuredName")},
                             "matchesP12": local is not None and local["_der"] == embedded["_der"]}
                report["certificates"] = [public_certificate(remote[0])]
        full, identities = [], set()
        for device in item["deviceList"]:
            actual = devices[device["id"]]
            visible = device.get("udid")
            if (device["id"] in identities or not isinstance(visible, str) or len(visible) != len(actual["udid"])
                    or any(char != "*" and char != actual["udid"][index] for index, char in enumerate(visible))
                    or str(device.get("deviceType")) != str(actual["deviceType"]) or str(actual["deviceType"]) != "4"):
                raise ProfileMismatch("设备不匹配")
            identities.add(device["id"])
            full.append(actual)
        actual_udids = debug.get("device-ids")
        if (profile.get("type") != "release" or info.get("bundle-name") != settings["bundleName"]
                or str(item.get("provisionType")) != "6" or str(item.get("tempFlag")) != "0"
                or item.get("appId") != settings["appId"] or item.get("certName") != settings["certName"]
                or len(remote) != 1 or embedded["_der"] != remote[0]["_der"]
                or debug.get("device-id-type") != "udid" or not full or not isinstance(actual_udids, list)
                or len(set(actual_udids)) != len(actual_udids) or set(actual_udids) != {device["udid"] for device in full}):
            raise ProfileMismatch("Profile 身份或设备冲突")
        signer = remote[0]
        result.update(certificateIds=[signer["id"]], deviceCount=len(full), identityVerified=True, materialReady=signer["matchesP12"], teamEvidence="配置 Team；CMS 不提供已核验 Developer Team ID")
        # 仅当前 Jenkins 批准材料和远端内容相同，才把设备列为可复用来源。
        bound_profile, bound_devices = os.environ.get("HARMONY_PROFILE_FILE"), os.environ.get("HARMONY_DEVICES_FILE")
        approved = False
        if bound_profile and bound_devices:
            roster = json.loads(ios.restricted(bound_devices))
            approved = (ios.restricted(bound_profile) == content and isinstance(roster, list)
                        and all(str(entry.get("deviceType")) == "4" for entry in roster)
                        and {entry["udid"] for entry in roster} == {device["udid"] for device in full} and len(roster) == len(full))
        result["approvedMaterialsMatch"] = approved
        result["materialReady"] = signer["matchesP12"] and approved
        result["approvedForReuse"] = approved and signer["matchesP12"] and signer["status"] in ("valid", "expiring") and result["status"] in ("valid", "expiring")
        record_devices(report, "harmony", [{"name": device.get("deviceName", ""), "udid": device["udid"], "deviceType": 4} for device in full],
                       {"brand": row["brand"], "team": row["team"], "profile": settings["profileName"]}, result["approvedForReuse"])
    except ProfileMismatch:
        result.update(status="mismatch", identityVerified=False, materialReady=False)
    except Exception:
        result.update(status="unknown", identityVerified=False, materialReady=False)
        report["errors"].append("Profile 查询或解析未完成；状态未知，未输出原始响应")


def collect(config, brand, platform, state_root, credential_status):
    if platform not in ("android", "ios", "harmony"):
        raise ValueError("collect 必须选择一个平台")
    row = targets(config, platform, brand)[0]
    report = {key: row[key] for key in PUBLIC_TARGET if key in row}
    report.update(collectedAt=datetime.now(timezone.utc).isoformat(), queryStatus="not-configured",
                  certificates=[], profiles=[], devices=[], credentials=[], errors=[], materialStatus="missing")
    statuses = credential_status or {}
    for role, identity in row["credentialIds"].items():
        status = statuses.get(identity, "missing")
        report["credentials"].append({"role": role, "id": identity, "status": status if status in ("configured", "missing", "wrong-type") else "unknown"})
    if row["configurationStatus"] != "configured":
        return report
    if platform == "android":
        report["materialSource"] = "Jenkins"
        path, secret = os.environ.get("ANDROID_KEYSTORE_FILE"), os.environ.get("ANDROID_SIGNING_CONFIG_FILE")
        if not path and not secret:
            report["queryStatus"] = "partial"
            report["errors"].append("Android 签名材料未绑定；仅显示配置与凭据状态")
            return public_report(report)
        try:
            if not path or not secret or any(statuses.get(identity) == "wrong-type" for identity in row["credentialIds"].values()):
                raise ValueError("Android 签名凭据缺项或类型错误")
            settings = json.loads(Path(config).read_text(encoding="utf-8"))[brand]["android"]["signing"]
            info = android_signing.inspect_certificate(Path(path), Path(secret), row["keyAlias"], settings["certificateSha256"])
            info["status"] = validity(info["startsAt"], info["expiresAt"]) if info["matchesExpectedSha256"] else "mismatch"
            report["certificates"] = [info]
            report["materialStatus"] = "configured" if info["matchesExpectedSha256"] else "invalid"
            report["queryStatus"] = "complete"
        except Exception:
            report["queryStatus"], report["materialStatus"] = "unknown", "invalid"
            report["errors"].append("Android 签名材料校验失败；未输出原始材料或密码")
        return public_report(report)
    local = None
    try:
        local = material_certificate(platform, config, row, statuses)
        if local is not None and platform == "ios" and local["teamId"] != row["teamId"]:
            raise ValueError("P12 Team 不匹配")
        report["materialStatus"] = "configured" if local else "missing"
        report["materialSource"] = local.get("_source", "Jenkins") if local else "missing"
    except Exception:
        local = None
        report["materialStatus"] = "invalid"
        report["errors"].append("绑定签名材料校验失败；未输出原始材料")
    report["_stateRoot"] = Path(state_root)
    try:
        if platform == "ios":
            _ios_inventory(row, local, report)
        else:
            _harmony_inventory(row, local, report, config)
        report["queryStatus"] = "partial" if report["errors"] else "complete"
    except Exception:
        report["queryStatus"] = "unknown"
        report["errors"].append("供应商查询或材料解析失败；未输出原始响应")
        known_channels = {item["channel"] for item in report["profiles"]}
        report["profiles"].extend({"channel": channel, "name": name, "status": "unknown"}
                                  for channel, name in row["profileNames"].items() if channel not in known_channels)
    finally:
        report.pop("_stateRoot", None)
    return public_report(report)


def record_devices(report, platform, devices, source, approved):
    private = [dict(signing_devices.normalize_device(platform, device),
                    id=signing_devices.device_id(platform, device["udid"]), sources=[source]) for device in devices]
    report["devices"].extend(signing_devices._public(private))
    if approved:
        signing_devices.merge_catalog(report["_stateRoot"], platform, devices, source)


LABELS = {"configured": "已配置", "pending": "待接入", "not-configured": "待接入", "missing": "缺失",
          "valid": "有效", "expiring": "即将过期", "expired": "已过期", "not-yet-valid": "尚未生效",
          "unknown": "查询失败", "partial": "部分查询未完成", "complete": "已查询", "invalid": "材料无效", "mismatch": "不匹配",
          "conflict": "冲突", "wrong-type": "类型错误", "repository": "受控仓库", "Jenkins": "Jenkins",
          "apiKey": "API 私钥", "apiKeyId": "API Key 标识", "apiIssuerId": "API Issuer 标识",
          "apiSubAccount": "API 子账号", "p12": "证书 P12", "p12Password": "P12 密码", "profile": "Profile", "devices": "设备名单", "keystore": "Android Keystore", "signingConfig": "Android 签名配置",
          "pgyer": "Ad Hoc 测试", "testflight": "TestFlight", "appstore": "App Store", "designated": "指定设备"}


def display_date(value):
    if not value:
        return "未知"
    try:
        return utc(value).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M +08:00")
    except (ValueError, TypeError):
        return "未知"


def public_report(row):
    # 归档只取明确字段；原始 API 响应、私有设备行和凭据 value 不能由报告输入带入。
    result = {key: row[key] for key in PUBLIC_TARGET if key in row}
    for key in ("collectedAt", "queryStatus", "materialStatus", "materialSource", "errors"):
        if key in row:
            result[key] = row[key]
    fields = {"certificates": ("id", "type", "name", "subject", "issuer", "configuredName", "serial", "sha256", "startsAt", "expiresAt", "status", "teamId", "matchesP12", "matchesExpectedSha256"),
              "profiles": ("id", "channel", "name", "type", "state", "status", "startsAt", "expiresAt", "devicePolicy", "deviceCount", "certificateIds", "cmsSignatureVerified", "caTrustVerified", "identityVerified", "materialReady", "approvedMaterialsMatch", "approvedForReuse", "teamEvidence"),
              "credentials": ("role", "id", "status"), "devices": ("id", "name", "maskedUdid", "deviceType", "sources")}
    for group, allowed in fields.items():
        result[group] = [{key: value[key] for key in allowed if key in value} for value in row.get(group, [])]
    for device in result["devices"]:
        device["sources"] = [{key: source[key] for key in ("brand", "team", "profile") if key in source}
                             for source in device.get("sources", [])]
    return result


def render(reports, state_root, output_dir):
    rows = []
    for path in sorted(Path(reports).glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(row, dict) or row.get("platform") not in ("android", "ios", "harmony"):
            raise ValueError("台账报告结构无效")
        rows.append(public_report(row))
    devices = {platform: signing_devices.public_catalog(Path(state_root), platform) for platform in ("ios", "harmony")}
    document = {"generatedAt": datetime.now(timezone.utc).isoformat(), "brands": rows, "devices": devices}
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "inventory.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    escape = lambda value: html.escape(str(value), quote=True)
    label = lambda value: escape(LABELS.get(value, value))
    platform_label = {"android": "Android", "ios": "iOS", "harmony": "鸿蒙"}
    anchors = {index: "brand-" + str(index) for index in range(len(rows))}
    parts = ["<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><link rel=\"stylesheet\" href=\"report.css\"><title>品牌签名与测试设备</title></head><body>",
             "<h1>品牌签名与测试设备</h1><p>更新时间：" + escape(display_date(document["generatedAt"])) + "（北京时间）</p>",
             "<p>材料核对不替代签包、真机安装验收；TestFlight / App Store 不限制设备。查询失败的项须重新核对。</p>",
             "<p>品牌设备名单来自当前 Profile；可供新品牌复用的设备见独立的已批准测试设备列表。</p>",
             '<p><a href="#android">Android</a> · <a href="#ios">iOS</a> · <a href="#harmony">HarmonyOS</a> · <a href="#devices">已批准测试设备</a></p>']
    parts.append("<h2>品牌汇总</h2><div class=\"table-wrap\"><table><thead><tr><th>品牌</th><th>平台</th><th>团队</th><th>材料 / 查询</th><th>最近到期（+08:00）</th><th>Profile 设备数</th><th>详情</th></tr></thead><tbody>")
    for index, row in enumerate(rows):
        dates = [utc(item["expiresAt"]) for item in row.get("certificates", []) + row.get("profiles", []) if item.get("expiresAt") and item.get("status") != "unknown"]
        cells = [escape(row.get("displayName", row["brand"])), platform_label[row["platform"]], escape(row.get("teamId") or row.get("team") or "待确认"),
                 label(row.get("materialStatus", "missing")) + " / " + label(row.get("queryStatus", "unknown")), escape(display_date(min(dates).isoformat()) if dates else "未知"),
                 "不限制设备" if row["platform"] == "android" else str(len({device["id"] for device in row.get("devices", [])})), '<a href="#' + anchors[index] + '">查看</a>']
        parts.append("<tr>" + "".join("<td>" + cell + "</td>" for cell in cells) + "</tr>")
    parts.append("</tbody></table></div>")
    for platform in ("android", "ios", "harmony"):
        parts.append('<h2 id="' + platform + '">' + platform_label[platform] + "</h2>")
        for index, row in enumerate(rows):
            if row["platform"] != platform:
                continue
            parts.append('<h3 id="' + anchors[index] + '">' + escape(row.get("displayName", row["brand"])) + " (" + escape(row["brand"]) + ")</h3>")
            parts.append("<p>团队：" + escape(row.get("teamId") or row.get("team") or "待确认") + "；公司：" + escape(row.get("companyName", "")) + "；应用：" + escape(row.get("applicationId") or row.get("bundleId") or row.get("bundleName", "")) + "；查询：" + label(row.get("queryStatus", "unknown")) + "；材料：" + label(row.get("materialStatus", "missing")) + "；来源：" + label(row.get("materialSource", "missing")) + "；签名入口：" + ("已启用" if row.get("enabled") else "未启用") + "</p>")
            parts.append("<p>密钥保管归属：" + escape(row.get("keyOwner") or "未登记") + "；Key alias：" + escape(row.get("keyAlias") or "不适用") + "。证书主体为证书中登记的身份，不作为法律归属证明。</p>")
            if platform == "android":
                parts.append("<p>Android 不使用 Profile 或设备白名单；本页仅提供签名查询。</p>")
            parts.append("<div class=\"table-wrap\"><table><thead><tr><th>类型</th><th>名称 / ID</th><th>渠道 / 状态</th><th>有效期（+08:00）</th><th>设备</th></tr></thead><tbody>")
            for kind, entries in (("团队证书", row.get("certificates", [])), ("Profile", row.get("profiles", []))):
                for entry in entries:
                    cells = [kind, str(entry.get("configuredName") or entry.get("name", "")) + " / " + str(entry.get("id", "")), LABELS.get(entry.get("channel"), entry.get("type", "")) + " / " + LABELS.get(entry.get("status"), "未知"), display_date(entry.get("startsAt")) + " → " + display_date(entry.get("expiresAt")), entry.get("deviceCount", "")]
                    if entry.get("materialReady") is False or entry.get("matchesP12") is False:
                        cells[2] += " / 签名材料未就绪"
                    elif entry.get("materialReady") is True or entry.get("matchesP12") is True:
                        cells[2] += " / 签名材料匹配"
                    if entry.get("certificateIds"):
                        cells[1] += "；授权证书：" + ", ".join(entry["certificateIds"])
                    if kind == "团队证书":
                        cells[1] += "；签发者：" + entry.get("issuer", "未知") + "；SHA-256：" + entry.get("sha256", "未知")
                    if entry.get("deviceCount") and entry.get("approvedForReuse") is False:
                        cells[2] += " / 设备仅供查询，未加入共享池"
                    parts.append("<tr>" + "".join("<td>" + escape(cell) + "</td>" for cell in cells) + "</tr>")
            parts.append("</tbody></table></div><ul>")
            for item in row.get("credentials", []):
                status = LABELS.get(item["status"], item["status"])
                if row.get("materialSource") == "repository" and item["role"] in ("p12", "p12Password") and item["status"] == "missing":
                    status = "Jenkins 未保存；当前从受控仓库读取"
                parts.append("<li>" + label(item["role"]) + "：" + escape(status) + "；ID：" + escape(item["id"]) + "</li>")
            for note in row.get("configurationNotes", []) + row.get("errors", []):
                parts.append("<li>" + escape(note) + "</li>")
            parts.append("</ul>")
            if row.get("devices"):
                parts.append("<p>当前 Profile 中的测试设备：</p><ul>")
                for device in row["devices"]:
                    parts.append("<li>" + escape(device["name"]) + "（" + escape(device["maskedUdid"]) + "）</li>")
                parts.append("</ul>")
    parts.append('<h2 id="devices">已批准测试设备</h2><p>新品牌可用设备 ID 选择已批准手机；各团队分别登记并使用独立证书。以下来源保留批准记录，不代表本次仍可安装。</p>')
    for platform, entries in devices.items():
        parts.append("<h3>" + platform_label[platform] + "</h3><div class=\"table-wrap\"><table><thead><tr><th>设备 ID</th><th>名称</th><th>UDID（脱敏）</th><th>批准来源</th></tr></thead><tbody>")
        for device in entries:
            source = "; ".join(str(item["brand"]) + " / " + str(item["team"]) + " / " + str(item["profile"]) for item in device["sources"])
            parts.append("<tr>" + "".join("<td>" + escape(cell) + "</td>" for cell in (device["id"], device["name"], device["maskedUdid"], source)) + "</tr>")
        parts.append("</tbody></table></div>")
    parts.append("</body></html>")
    (output / "index.html").write_text("\n".join(parts) + "\n", encoding="utf-8")
    (output / "report.css").write_text("body{font:16px/1.6 system-ui,sans-serif;max-width:1200px;margin:24px auto;padding:0 16px;color:#202124}table{border-collapse:collapse;width:100%;min-width:760px;margin:16px 0}th,td{border:1px solid #dadce0;padding:10px;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#f1f3f4}tr:nth-child(even){background:#fafafa}.table-wrap{overflow-x:auto}a{color:#174ea6}h2{margin-top:40px}h3{margin-top:28px}p,li{overflow-wrap:anywhere}\n", encoding="utf-8")
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "collect", "render"))
    parser.add_argument("--config", type=Path, default=ROOT / "ci/brands.json")
    parser.add_argument("--platform", choices=("all", "android", "ios", "harmony"), default="all")
    parser.add_argument("--brands", default="all")
    parser.add_argument("--brand")
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--credential-status-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reports-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            print(json.dumps(targets(args.config, args.platform, args.brands), ensure_ascii=False))
        elif args.command == "collect":
            if not args.brand or not args.state_root or not args.output or args.output.resolve() == args.config.resolve():
                raise ValueError("collect 缺少品牌、目录或安全报告路径")
            public_destination(args.output, args.state_root, (args.config, args.credential_status_file))
            status = json.loads(args.credential_status_file.read_text()) if args.credential_status_file else {}
            report = collect(args.config, args.brand, args.platform, args.state_root, status)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            if not args.reports_dir or not args.state_root or not args.output_dir:
                raise ValueError("render 缺少目录")
            for name in ("index.html", "inventory.json", "report.css"):
                public_destination(args.output_dir / name, args.state_root, (args.config, args.credential_status_file))
            document = render(args.reports_dir, args.state_root, args.output_dir)
            print(json.dumps({"brands": len(document["brands"]),
                              "queryFailures": sum(row.get("queryStatus") in ("unknown", "partial") for row in document["brands"]),
                              "devices": {platform: len(devices) for platform, devices in document["devices"].items()}}, ensure_ascii=False))
        return 0
    except Exception:
        print("签名台账失败：参数、查询或报告无效；未输出 Secret/UDID", file=sys.stderr)
        return 1


def public_destination(path, state_root, protected):
    resolved = path.resolve()
    secret_root = state_root.resolve()
    if resolved == secret_root or secret_root in resolved.parents or any(value is not None and resolved == value.resolve() for value in protected):
        raise ValueError("公开报告不能覆盖配置或私有签名目录")
    if any(value.is_symlink() for value in (path, *path.parents)):
        raise ValueError("报告输出不能包含符号链接")


if __name__ == "__main__":
    sys.exit(main())
