#!/usr/bin/env python3
"""只读验证 IPA 的签名、品牌与分发 Profile，报告不包含设备标识或签名秘密。"""

import argparse
import base64
import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from delivery.ios_release import (
    DEFAULT_CONFIG, IosReleaseError, load_ios_brand_config,
    validate_ipa_build_settings, validate_ipa_ui_engine,
)


class SignatureError(RuntimeError):
    """签名验证失败；错误消息不能引用原始 native 工具输出。"""


def require(condition, message):
    if not condition:
        raise SignatureError(message)


def native(command, label):
    try:
        result = subprocess.run(command, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SignatureError(label + "：工具不可用或超时") from error
    require(result.returncode == 0, label + "失败")
    return result


def validate_archive(ipa):
    """先校验路径与链接再交给 ditto，避免 ZIP 或符号链接越过临时目录。"""
    with zipfile.ZipFile(ipa) as archive:
        entries, links, apps = {}, {}, set()
        for entry in archive.infolist():
            name = entry.filename
            # Apple 导出 ZIP 可能未设置 UTF-8 flag，ditto 仍按 UTF-8 解码文件名。
            if not entry.flag_bits & 0x800:
                try:
                    name = name.encode("cp437").decode("utf-8")
                except UnicodeError:
                    pass
            name = unicodedata.normalize("NFD", name.rstrip("/"))
            parts = name.split("/")
            require(name and not entry.filename.startswith("/")
                    and "\\" not in name and ":" not in name
                    and all(part not in ("", ".", "..") for part in parts)
                    and entry.orig_filename == entry.filename,
                    "IPA 包含不安全 ZIP 路径")
            require(name not in entries, "IPA 包含重复 ZIP 条目")
            require(not entry.flag_bits & 1, "IPA 包含加密 ZIP 条目")
            kind = stat.S_IFMT(entry.external_attr >> 16)
            require(kind in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK),
                    "IPA 包含不支持的 ZIP 文件类型")
            entries[name] = kind
            if len(parts) >= 2 and parts[0] == "Payload" and parts[1].endswith(".app"):
                apps.add("/".join(parts[:2]))
            if kind == stat.S_IFLNK:
                require(entry.file_size <= 4096, "IPA 符号链接过长")
                target = archive.read(entry).decode("utf-8")
                require(target and not target.startswith("/")
                        and "\\" not in target and ":" not in target and "\x00" not in target,
                        "IPA 包含不安全符号链接")
                links[name] = unicodedata.normalize("NFD", target)
        require(len(apps) == 1, "IPA 必须包含唯一 Payload 根 App")
        app = next(iter(apps))
        require(entries.get(app, stat.S_IFDIR) in (0, stat.S_IFDIR)
                and app + "/Info.plist" in entries,
                "IPA 根 App 或 Info.plist 无效")
        for name in entries:
            require(not any(str(parent) in links for parent in PurePosixPath(name).parents),
                    "IPA ZIP 条目穿过符号链接")
        for name, target in links.items():
            pending = list(PurePosixPath(name).parent.parts) + target.split("/")
            resolved, expansions = [], 0
            while pending:
                part = pending.pop(0)
                if part in ("", "."):
                    continue
                if part == "..":
                    require(resolved, "IPA 符号链接越过解压目录")
                    resolved.pop()
                    continue
                candidate = "/".join(resolved + [part])
                if candidate in links:
                    expansions += 1
                    require(expansions <= 40, "IPA 符号链接循环或层级过深")
                    pending = links[candidate].split("/") + pending
                else:
                    resolved.append(part)
        return app


def utc(value):
    require(isinstance(value, datetime), "签名有效期字段无效")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def entitlement_allowed(actual, allowed):
    if isinstance(actual, str) and isinstance(allowed, str):
        # Apple Profile 使用 *；其他 glob 元字符不能放宽授权。
        return re.fullmatch(re.escape(allowed).replace(r"\*", ".*"), actual) is not None
    if isinstance(actual, list) and isinstance(allowed, list):
        return all(any(entitlement_allowed(item, permit) for permit in allowed) for item in actual)
    if isinstance(actual, list) and allowed == "*":
        return all(isinstance(item, str) for item in actual)
    if isinstance(actual, dict) and isinstance(allowed, dict):
        return all(key in allowed and entitlement_allowed(value, allowed[key])
                   for key, value in actual.items())
    return type(actual) is type(allowed) and actual == allowed


def validate_profile(info, entitlements, profile, ios, signer_der, now,
                     expected_version=None, expected_build=None):
    require(info.get("CFBundleIdentifier") == ios.bundle_id, "IPA Bundle ID 与品牌配置不一致")
    version, build = info.get("CFBundleShortVersionString"), info.get("CFBundleVersion")
    require(isinstance(version, str) and version and isinstance(build, str) and build,
            "IPA 缺少版本或构建号")
    require(not expected_version or version == expected_version, "IPA 版本与预期不一致")
    require(not expected_build or build == expected_build, "IPA 构建号与预期不一致")
    require(profile.get("Name") == ios.profile_name, "IPA Profile 名称与品牌配置不一致")
    require(profile.get("TeamIdentifier") == [ios.team_id], "IPA Profile Team 与品牌配置不一致")
    require(isinstance(profile.get("DeveloperCertificates"), list)
            and signer_der in profile["DeveloperCertificates"],
            "IPA 签名证书未获 Profile 授权")
    require(utc(profile.get("CreationDate")) <= now < utc(profile.get("ExpirationDate")),
            "IPA Profile 尚未生效或已过期")
    allowed = profile.get("Entitlements")
    require(isinstance(allowed, dict) and isinstance(entitlements, dict), "IPA Entitlements 无效")
    require(entitlements.get("get-task-allow", False) is False
            and allowed.get("get-task-allow", False) is False,
            "分发 IPA 不允许 get-task-allow")
    require(entitlements.get("com.apple.developer.team-identifier") == ios.team_id
            and allowed.get("com.apple.developer.team-identifier") == ios.team_id,
            "IPA Entitlements Team 不一致")
    prefixes = profile.get("ApplicationIdentifierPrefix")
    require(isinstance(prefixes, list) and prefixes
            and entitlements.get("application-identifier") in
            [prefix + "." + ios.bundle_id for prefix in prefixes if isinstance(prefix, str)],
            "IPA application-identifier 与品牌不一致")
    require("application-identifier" in allowed, "Profile 缺少 application-identifier")
    require(all(key in allowed and entitlement_allowed(value, allowed[key])
                for key, value in entitlements.items()),
            "IPA Entitlements 超出 Profile 授权")
    devices = profile.get("ProvisionedDevices")
    require(profile.get("ProvisionsAllDevices", False) is False,
            "不允许 Enterprise Profile")
    if ios.distribution == "adhoc":
        require(isinstance(devices, list) and devices and all(isinstance(d, str) and d for d in devices),
                "Ad Hoc Profile 缺少测试设备")
    else:
        require(ios.distribution == "appstore" and "ProvisionedDevices" not in profile
                and allowed.get("beta-reports-active") is True,
                "App Store 分发必须使用 App Store Profile")
    return version, build, len(devices or [])


def verify_signature(args):
    ios = load_ios_brand_config(args.config, args.brand, args.pipeline, args.channel, args.ui_engine)
    for path in (args.ipa, args.p12, args.password_file):
        require(path.is_file(), "缺少 IPA、P12 或密码文件")
    require(args.output.resolve() not in
            {path.resolve() for path in (args.ipa, args.p12, args.password_file, args.config)},
            "报告不能覆盖输入文件")
    app_entry = validate_archive(args.ipa)
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="ios-signature-") as directory:
        temporary = Path(directory)
        temporary.chmod(0o700)
        native(["/usr/bin/ditto", "-xk", str(args.ipa.resolve()), str(temporary / "ipa")], "IPA 解压")
        app = temporary / "ipa" / app_entry
        require(app.is_dir() and not app.is_symlink(), "IPA 根 App 无效")
        native(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], "codesign deep strict 校验")
        identity = native(["/usr/bin/codesign", "-d", "--verbose=4", str(app)], "签名身份读取")
        fields = dict(re.findall(r"^(Identifier|TeamIdentifier)=(.*)$", identity.stderr.decode("utf-8"), re.M))
        require(fields.get("Identifier") == ios.bundle_id and fields.get("TeamIdentifier") == ios.team_id,
                "codesign Team 或 Bundle ID 与品牌不一致")
        prefix = temporary / "signer"
        native(["/usr/bin/codesign", "-d", "--extract-certificates=" + str(prefix), str(app)], "签名证书读取")
        signer_file = Path(str(prefix) + "0")
        signer_file.chmod(0o600)
        signer_der = signer_file.read_bytes()
        certificate = native(["/usr/bin/openssl", "pkcs12", "-in", str(args.p12.resolve()),
                              "-passin", "file:" + str(args.password_file.resolve()), "-clcerts", "-nokeys"],
                             "P12 公开证书读取").stdout
        certs = re.findall(rb"-----BEGIN CERTIFICATE-----\s*(.*?)\s*-----END CERTIFICATE-----", certificate, re.S)
        require(len(certs) == 1 and base64.b64decode(certs[0]) == signer_der,
                "IPA 签名证书与 P12 不一致或 P12 身份不唯一")
        public_cert = temporary / "signer.pem"
        with public_cert.open("xb") as stream:
            os.chmod(public_cert, 0o600)
            stream.write(certificate)
        cert_details = native(["/usr/bin/openssl", "x509", "-in", str(public_cert),
                               "-noout", "-dates", "-subject", "-nameopt", "RFC2253"], "证书有效期读取").stdout.decode("utf-8")
        dates = dict(re.findall(r"^(notBefore|notAfter|subject)=(.*)$", cert_details, re.M))
        starts, expires = [datetime.strptime(dates[key], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                           for key in ("notBefore", "notAfter")]
        require(starts <= now < expires, "P12 签名证书尚未生效或已过期")
        require(re.search(r"(?:^|,)OU=" + re.escape(ios.team_id) + r"(?:,|$)", dates["subject"].strip())
                and re.search(r"(?:^|,)CN=(?:Apple Distribution|iPhone Distribution):", dates["subject"].strip()),
                "P12 必须为当前 Team 的分发证书")
        cms_command = ["/usr/bin/security", "cms", "-D", "-i", str(app / "embedded.mobileprovision")]
        keychain_path = os.environ.get("IOS_KEYCHAIN_PATH")
        if keychain_path:
            require(Path(keychain_path).is_file(), "指定的 CMS 钥匙串文件不存在")
            cms_command += ["-k", keychain_path]
        profile = plistlib.loads(native(cms_command, "内嵌 Profile 解码").stdout)
        entitlements = plistlib.loads(native(["/usr/bin/codesign", "-d", "--entitlements", ":-", str(app)],
                                            "Entitlements 读取").stdout)
        info = plistlib.loads((app / "Info.plist").read_bytes())
        require(isinstance(info, dict) and isinstance(profile, dict), "IPA plist 必须为字典")
        version, build, count = validate_profile(info, entitlements, profile, ios, signer_der, now,
                                                 args.expected_version, args.expected_build)
        validate_ipa_build_settings(args.ipa, ios)
        validate_ipa_ui_engine(args.ipa, ios.ui_engine)
    with args.ipa.open("rb") as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "verified": True, "brand": ios.key, "pipeline": ios.pipeline,
        "channel": args.channel or ("pgyer" if ios.pipeline == "test" else "appstore"),
        "distribution": ios.distribution,
        "uiEngine": ios.ui_engine, "version": version, "build": build,
        "bundleId": ios.bundle_id, "teamId": ios.team_id, "profile": ios.profile_name,
        "profileExpiresAt": utc(profile["ExpirationDate"]).isoformat(),
        "certificateExpiresAt": expires.isoformat(),
        "expiryWarnings": [label + "将在 30 天内过期" for label, expiration in
                           (("签名证书", expires), ("Profile", utc(profile["ExpirationDate"])))
                           if (expiration - now).total_seconds() <= 30 * 86400],
        "signerMatchesP12": True, "profileAuthorizesSigner": True,
        "codesignDeepStrictVerified": True, "registeredDeviceCount": count,
        "ipaBytes": args.ipa.stat().st_size, "sha256": digest.hexdigest(),
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brand", required=True)
    parser.add_argument("--pipeline", required=True, choices=("test", "release"))
    parser.add_argument("--channel", choices=("pgyer", "testflight"), default="")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--ui-engine", choices=("cmp", "kuikly", "native"), default=os.environ.get("CI_UI_ENGINE", "cmp"))
    for flag in ("ipa", "p12", "password-file", "output"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--expected-version")
    parser.add_argument("--expected-build", "--expected-build-number", dest="expected_build")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = verify_signature(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output.parent,
                                         prefix=".signature-report-", delete=False) as stream:
            report_file = Path(stream.name)
            try:
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.close()
                os.replace(report_file, args.output)
            finally:
                report_file.unlink(missing_ok=True)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except SignatureError as error:
        print("iOS 签名验证失败：" + str(error), file=sys.stderr)
    except (IosReleaseError, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile,
            plistlib.InvalidFileException, UnicodeError):
        print("iOS 签名验证失败：品牌配置、文件或签名数据无效", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
