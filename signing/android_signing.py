#!/usr/bin/env python3
"""仅注入所选 Android 品牌的 Jenkins 签名，并严格核对 APK 证书。"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from project import SOURCE_ROOT, android_contract
from project_config import ProjectConfigError


ROOT = SOURCE_ROOT


class AndroidSigningError(RuntimeError):
    pass


def normalize_sha256(value):
    fingerprint = str(value).replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise AndroidSigningError("Android 签名证书 SHA-256 必须为 64 位十六进制")
    return fingerprint


def load_signing(config, brand):
    if not re.fullmatch(r"[a-z][a-z0-9_]*", brand):
        raise AndroidSigningError("Android 品牌参数无效")
    try:
        signing = json.loads(Path(config).read_text())[brand]["android"]["signing"]
        credentials = signing["credentials"]
        if (signing["source"] != "jenkins" or not isinstance(signing["keyAlias"], str)
                or not signing["keyAlias"] or "\0" in signing["keyAlias"]):
            raise ValueError()
        normalize_sha256(signing["certificateSha256"])
        for field in ("keystore", "signingJson"):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", credentials[field]):
                raise ValueError()
        return signing
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise AndroidSigningError("所选品牌缺少有效的 Jenkins Android 签名配置") from error


def load_passwords(path):
    try:
        value = json.loads(Path(path).read_text())
        if any(not isinstance(value.get(key), str) or not value[key] or "\0" in value[key]
               for key in ("storePassword", "keyPassword")):
            raise ValueError()
        return value
    except (OSError, ValueError, AttributeError) as error:
        raise AndroidSigningError("Android 签名密码 JSON 无效") from error


def checked_output(command, *, environment=None, input_text=None):
    try:
        result = subprocess.run(command, env=environment, input=input_text,
                                text=True, capture_output=True, check=False)
    except OSError as error:
        raise AndroidSigningError("Android 签名工具不可用：" + Path(command[0]).name) from error
    if result.returncode:
        # 厂商工具可能把敏感输入写入错误输出，不透传原始 stderr。
        raise AndroidSigningError("Android 签名材料校验失败：" + Path(command[0]).name)
    return result.stdout


def inspect_certificate(keystore: Path, signing_json: Path, key_alias: str,
                        expected_sha256: str = "") -> dict:
    passwords = load_passwords(signing_json)
    environment = dict(os.environ, ANDROID_STORE_PASSWORD=passwords["storePassword"],
                       ANDROID_KEY_PASSWORD=passwords["keyPassword"],
                       ANDROID_CERTIFICATE_PASSWORD=secrets.token_hex(24))
    with tempfile.TemporaryDirectory(prefix="android-certificate-") as temporary:
        exported = Path(temporary) / "private-key.p12"
        # 按 alias 导出私钥可同时拒绝 trustedCertEntry 和错误 keyPassword。
        checked_output([
            "keytool", "-importkeystore", "-noprompt", "-srckeystore", str(keystore),
            "-srcalias", key_alias, "-srcstorepass:env", "ANDROID_STORE_PASSWORD",
            "-srckeypass:env", "ANDROID_KEY_PASSWORD", "-destkeystore", str(exported),
            "-deststoretype", "PKCS12", "-deststorepass:env", "ANDROID_CERTIFICATE_PASSWORD",
        ], environment=environment)
        exported.chmod(0o600)
        pem = checked_output([
            "openssl", "pkcs12", "-in", str(exported), "-passin",
            "env:ANDROID_CERTIFICATE_PASSWORD", "-clcerts", "-nokeys",
        ], environment=environment)
        if pem.count("-----BEGIN CERTIFICATE-----") != 1:
            raise AndroidSigningError("所选 Android alias 必须导出唯一私钥证书")
        output = checked_output([
            "openssl", "x509", "-noout", "-subject", "-issuer", "-dates",
            "-fingerprint", "-sha256", "-nameopt", "RFC2253,utf8,-esc_msb",
        ], input_text=pem)
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    try:
        fingerprint = normalize_sha256(next(value for key, value in fields.items()
                                            if key.lower().endswith("fingerprint")))
        starts = datetime.strptime(fields["notBefore"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        expires = datetime.strptime(fields["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        subject, issuer = fields["subject"], fields["issuer"]
    except (KeyError, ValueError, StopIteration) as error:
        raise AndroidSigningError("Android 证书公开信息解析失败") from error
    matches = bool(expected_sha256) and fingerprint == normalize_sha256(expected_sha256)
    now = datetime.now(timezone.utc)
    status = "expired" if expires <= now else "not-yet-valid" if starts > now else "valid"
    if expected_sha256 and not matches:
        status = "mismatch"
    return dict(name=subject, subject=subject, issuer=issuer, sha256=fingerprint,
                startsAt=starts.isoformat(), expiresAt=expires.isoformat(),
                status=status, matchesExpectedSha256=matches)


def property_value(value):
    # Java Properties 默认 ISO-8859-1，逐字符转义避免空格、换行和 Unicode 改变密码。
    escaped = []
    for character in str(value):
        if character in "\\ :=#!":
            escaped.append("\\" + character)
        elif character in "\n\r\t\f":
            escaped.append({"\n": "\\n", "\r": "\\r", "\t": "\\t", "\f": "\\f"}[character])
        elif ord(character) > 127:
            encoded = character.encode("utf-16-be")
            escaped.extend("\\u" + encoded[index:index + 2].hex() for index in range(0, len(encoded), 2))
        else:
            escaped.append(character)
    return "".join(escaped)


def find_apksigner():
    executable = shutil.which("apksigner")
    if executable:
        return executable
    sdk = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    if sdk:
        candidates = list((Path(sdk) / "build-tools").glob("*/apksigner"))
        candidates.sort(key=lambda path: tuple(int(part) for part in re.findall(r"\d+", path.parent.name)), reverse=True)
        if candidates:
            return str(candidates[0])
    raise AndroidSigningError("Android SDK 缺少 apksigner")


def verify_apk(apk, expected_sha256):
    output = checked_output([find_apksigner(), "verify", "--print-certs", str(apk)])
    fingerprints = re.findall(r"^Signer #\d+ certificate SHA-256 digest:\s*([0-9a-fA-F:]+)\s*$",
                              output, re.MULTILINE)
    expected = normalize_sha256(expected_sha256)
    if len(fingerprints) != 1 or normalize_sha256(fingerprints[0]) != expected:
        raise AndroidSigningError("APK 唯一签名证书与品牌配置 SHA-256 不一致")
    return expected


def build(args, signing):
    report_path = Path(args.output_dir) / "android-signature-report.json"
    report_path.unlink(missing_ok=True)
    if not (os.environ.get("JENKINS_URL") and os.environ.get("BUILD_NUMBER")
            and os.environ.get("ANDROID_SIGNING_SOURCE") == "jenkins"):
        raise AndroidSigningError("Android CI 签名只允许来源为 Jenkins 的凭据")
    try:
        keystore = Path(os.environ["ANDROID_KEYSTORE_FILE"]).resolve(strict=True)
        signing_json = Path(os.environ["ANDROID_SIGNING_CONFIG_FILE"]).resolve(strict=True)
    except (KeyError, OSError) as error:
        raise AndroidSigningError("Jenkins 未绑定所选品牌的 Android 签名文件") from error
    for path in (keystore, signing_json):
        if not path.is_file() or path.stat().st_mode & 0o077:
            raise AndroidSigningError("Jenkins Android 签名文件必须为仅 owner 可读的普通文件")
    certificate = inspect_certificate(keystore, signing_json, signing["keyAlias"], signing["certificateSha256"])
    if not certificate["matchesExpectedSha256"]:
        raise AndroidSigningError("Jenkins 私钥证书与品牌配置 SHA-256 不一致")
    passwords = load_passwords(signing_json)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise AndroidSigningError("缺少 Android 构建命令")
    contract = android_contract(args.brand, args.pipeline, args.ui_engine)
    module = contract["module"]
    apk_output = Path.cwd() / contract["outputDir"]
    receipt_path = Path.cwd() / module / "build/android-signing-source.properties"
    receipt_path.unlink(missing_ok=True)
    # 只清当前变体的 APK，保证报告来自本次构建并保留其他品牌与编译缓存。
    if apk_output.exists():
        try:
            shutil.rmtree(apk_output)
        except OSError as error:
            raise AndroidSigningError("无法清理所选 Android 变体的旧 APK 输出") from error
    with tempfile.TemporaryDirectory(prefix="android-signing-") as temporary:
        properties = Path(temporary) / "keystore.properties"
        values = dict(storeFile=str(keystore), keyAlias=signing["keyAlias"],
                      storePassword=passwords["storePassword"], keyPassword=passwords["keyPassword"])
        properties.touch(mode=0o600)
        properties.write_text("".join(f"signing.{args.brand}.{key}={property_value(value)}\n"
                                      for key, value in values.items()), encoding="ascii")
        environment = dict(os.environ, ANDROID_SIGNING_BRAND=args.brand,
                           ANDROID_SIGNING_PROPERTIES=str(properties), CI_UI_ENGINE=args.ui_engine)
        # 签名密码进入 AGP 模型后不能持久化到 Gradle Configuration Cache。
        environment["GRADLE_OPTS"] = os.environ.get("GRADLE_OPTS", "") + " -Dorg.gradle.configuration-cache=false"
        result = subprocess.run(command, env=environment, check=False)
        if result.returncode:
            raise AndroidSigningError("Android 构建失败，未生成本次签名验收报告")
        try:
            receipt = dict(line.split("=", 1) for line in receipt_path.read_text().splitlines()
                           if "=" in line and not line.startswith("#"))
        except OSError as error:
            raise AndroidSigningError("当前 Android 分支未提供 Jenkins SigningConfig 来源证明") from error
        keystore_sha256 = hashlib.sha256(keystore.read_bytes()).hexdigest()
        if receipt != dict(source="jenkins", brand=args.brand, keystoreSha256=keystore_sha256):
            raise AndroidSigningError("Android SigningConfig 来源证明与本次 Jenkins 材料不一致")
        apks = list(apk_output.glob("*.apk"))
        if len(apks) != 1:
            raise AndroidSigningError("所选 Android 品牌输出目录必须包含唯一 APK")
        apk = apks[0]
        fingerprint = verify_apk(apk, signing["certificateSha256"])
        report = dict(platform="android", brand=args.brand, source="jenkins", verified=True,
                      pipeline=args.pipeline, uiEngine=args.ui_engine, apk=str(apk),
                      apkSha256=hashlib.sha256(apk.read_bytes()).hexdigest(),
                      certificateSha256=fingerprint, keystoreSha256=keystore_sha256,
                      credentials=signing["credentials"],
                      commit=checked_output(["git", "rev-parse", "HEAD"]).strip())
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Android Jenkins signing verified: brand={args.brand} certificateSha256={fingerprint}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("credentials", "build"))
    parser.add_argument("--brand", required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "ci/brands.json")
    parser.add_argument("--pipeline", choices=("test", "release"), default="test")
    parser.add_argument("--ui-engine", choices=("cmp", "kuikly", "native"), default=os.environ.get("CI_UI_ENGINE", "cmp"))
    parser.add_argument("--output-dir", type=Path, default=Path("build/ci"))
    args, command = parser.parse_known_args()
    args.command = command
    try:
        signing = load_signing(args.config, args.brand)
        if args.action == "credentials":
            print(signing["credentials"]["keystore"])
            print(signing["credentials"]["signingJson"])
        else:
            build(args, signing)
    except (AndroidSigningError, ProjectConfigError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
