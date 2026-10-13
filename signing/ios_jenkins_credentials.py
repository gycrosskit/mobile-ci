#!/usr/bin/env python3
"""按品牌测试 Team 接入 Jenkins iOS 凭据；默认只读核对，--apply 才创建缺失项。"""

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from project import SOURCE_ROOT

from signing.ios_signing_setup import SigningError, openssl, p12_identity


DEFAULT_CONFIG = SOURCE_ROOT / "ci" / "brands.json"
SUFFIXES = {
    "distribution-p12": "Secret file",
    "distribution-p12-password": "Secret text",
    "asc-api-key": "Secret file",
    "asc-key-id": "Secret text",
    "asc-issuer-id": "Secret text",
}
MARKER = "IOS_CREDENTIAL_REPORT:"


class CredentialError(RuntimeError):
    """消息只含操作与状态，禁止包含原始响应或敏感输入。"""


def private_bytes(path, label, *, allowed_modes=(0o600,)):
    try:
        descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in allowed_modes:
                modes = "/".join("{:04o}".format(mode) for mode in allowed_modes)
                raise CredentialError("{} 必须是非符号链接的 {} 普通文件".format(label, modes))
            if info.st_uid != os.getuid():
                raise CredentialError("{} 必须由当前用户拥有".format(label))
            value = source.read(16 * 1024 * 1024 + 1)
    except (OSError, ValueError):
        raise CredentialError("无法安全读取{}；拒绝符号链接".format(label)) from None
    if not value or len(value) > 16 * 1024 * 1024:
        raise CredentialError("{} 为空或超出大小限制".format(label))
    return value


def private_json(path, label):
    try:
        value = json.loads(private_bytes(path, label))
    except (ValueError, UnicodeError):
        raise CredentialError("{} 必须是有效 JSON".format(label)) from None
    if not isinstance(value, dict):
        raise CredentialError("{} 必须是 JSON 对象".format(label))
    return value


def team_config(config_path, brand):
    try:
        root = json.loads(Path(config_path).read_text(encoding="utf-8"))
        team = root[brand]["ios"]["signing"]["test"]["team"]
        registered = root["_iosTeams"][team]
        team_id = registered["teamId"]
        namespace = registered["credentialNamespace"]
        if not re.fullmatch(r"[A-Z0-9]{10}", team_id):
            raise ValueError()
        if not re.fullmatch(r"[a-z][a-z0-9-]*", namespace):
            raise ValueError()
        if any(key != team and item.get("credentialNamespace") == namespace
               for key, item in root["_iosTeams"].items()):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise CredentialError("品牌测试 Team 或凭据命名空间未正确登记") from None
    return team, team_id, namespace


def credential_inputs(config_path, brand, p12=None, password_file=None, api_key_config=None):
    team, team_id, namespace = team_config(config_path, brand)
    descriptions = {
        "distribution-p12": "分发证书与私钥 | iOS 打包、签名维护消费；同 Team 复用",
        "distribution-p12-password": "P12 密码 | iOS 打包、签名维护消费；与 P12 成对维护",
        "asc-api-key": "App Store Connect API 私钥 | iOS 打包、签名维护消费；管理员预置",
        "asc-key-id": "App Store Connect Key ID | iOS 打包、签名维护消费；与 API 私钥配套",
        "asc-issuer-id": "App Store Connect Issuer ID | iOS 打包、签名维护消费；与 API 私钥配套",
    }
    materials = {}
    if bool(p12) != bool(password_file):
        raise CredentialError("--p12 与 --password-file 必须成对提供")
    if p12:
        materials["distribution-p12"] = (private_bytes(p12, "P12"), "distribution.p12")
        try:
            password = private_bytes(password_file, "P12 密码文件").decode("utf-8").rstrip("\r\n")
        except UnicodeError:
            raise CredentialError("P12 密码必须是 UTF-8 文本") from None
        if not password or any(character in password for character in "\r\n\0"):
            raise CredentialError("P12 密码必须是非空单行文本")
        try:
            _, identity = p12_identity(p12, password.encode("utf-8"))
        except (SigningError, OSError, ValueError):
            raise CredentialError("P12 密码、证书或唯一私钥校验失败") from None
        if identity["teamId"] != team_id:
            raise CredentialError("P12 证书 Team 与品牌登记不一致")
        if identity["expires"] <= datetime.now(timezone.utc):
            raise CredentialError("P12 证书已过期")
        if private_bytes(p12, "P12") != materials["distribution-p12"][0]:
            raise CredentialError("P12 校验期间文件发生变化")
        materials["distribution-p12-password"] = (password.encode("utf-8"), None)
    if api_key_config:
        config = private_json(api_key_config, "API Key 配置")
        key_id, issuer_id = config.get("keyId"), config.get("issuerId")
        if config.get("teamId") != team_id:
            raise CredentialError("API Key 配置 teamId 与品牌登记 Team 不一致")
        if not isinstance(key_id, str) or not re.fullmatch(r"[A-Z0-9]{10}", key_id):
            raise CredentialError("API Key keyId 无效")
        if not isinstance(issuer_id, str) or not re.fullmatch(
                r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", issuer_id):
            raise CredentialError("API Key issuerId 无效")
        if not isinstance(config.get("keyPath"), str) or not config["keyPath"]:
            raise CredentialError("API Key 配置缺少 keyPath")
        key_path = Path(config["keyPath"])
        if not key_path.is_absolute():
            key_path = Path(api_key_config).parent / key_path
        key = private_bytes(key_path, "API Key")
        if not key.strip().startswith(b"-----BEGIN PRIVATE KEY-----") or not key.strip().endswith(
                b"-----END PRIVATE KEY-----"):
            raise CredentialError("API Key 必须是 PEM 私钥")
        try:
            public_der = openssl("pkey", "-pubout", "-outform", "DER", data=key)
        except (SigningError, OSError):
            raise CredentialError("API Key 私钥校验失败") from None
        # SPKI 的 EC / prime256v1 OID 固定；避免不同 OpenSSL 版本的私钥文本格式差异。
        p256_prefix = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d03010703420004")
        if len(public_der) != 91 or not public_der.startswith(p256_prefix):
            raise CredentialError("API Key 必须为 ES256 P-256 密钥")
        materials["asc-api-key"] = (key, "AuthKey_{}.p8".format(key_id))
        materials["asc-key-id"] = (key_id.encode("utf-8"), None)
        materials["asc-issuer-id"] = (issuer_id.encode("utf-8"), None)
    items = []
    for suffix, kind in SUFFIXES.items():
        if materials and suffix not in materials:
            continue
        item = {"id": namespace + "-" + suffix, "type": kind,
                "description": "iOS | Team {}（{}） | {}".format(team, team_id, descriptions[suffix])}
        if suffix in materials:
            content, filename = materials[suffix]
            item.update(value=base64.b64encode(content).decode("ascii"),
                        sha256=hashlib.sha256(content).hexdigest(), filename=filename)
        items.append(item)
    return items


def auth_headers(auth_file=None, headers_command=None):
    if auth_file:
        auth = private_json(auth_file, "Jenkins 认证文件")
        if not all(isinstance(auth.get(key), str) and auth[key] for key in ("username", "apiToken")):
            raise CredentialError("Jenkins 认证文件必须包含 username 与 apiToken")
        if any(character in auth["username"] for character in ":\r\n"):
            raise CredentialError("Jenkins username 无效")
        token = base64.b64encode((auth["username"] + ":" + auth["apiToken"]).encode()).decode()
        headers = {"Authorization": "Basic " + token}
    elif headers_command:
        try:
            argv = shlex.split(headers_command)
            if not argv:
                raise ValueError()
            result = subprocess.run(argv, capture_output=True, timeout=30, check=True)
            headers = json.loads(result.stdout)
            headers = headers.get("headers", headers)
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            raise CredentialError("Jenkins 认证 helper 失败；输出已隐藏") from None
    else:
        raise CredentialError("必须提供 --auth-file 或 --headers-command")
    if not isinstance(headers, dict) or not headers:
        raise CredentialError("Jenkins 认证 headers 无效")
    if not any(key.lower() == "authorization" for key in headers if isinstance(key, str)):
        raise CredentialError("Jenkins 认证缺少 Authorization")
    for key, value in headers.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9-]+", key)
                or not isinstance(value, str) or "\r" in value or "\n" in value
                or key.lower() in ("host", "content-length", "content-type")):
            raise CredentialError("Jenkins 认证 headers 无效")
    return headers


class RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        # 即使同源也不跟随：scriptText 的未知结果只能由下一次核对恢复。
        raise CredentialError("Jenkins 重定向被拒绝")


class JenkinsClient:
    def __init__(self, url, headers):
        try:
            parsed = urllib.parse.urlsplit(url)
            parsed.port
        except ValueError:
            raise CredentialError("Jenkins URL 无效") from None
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise CredentialError("Jenkins URL 无效")
        self.url = url.rstrip("/")
        self.headers = headers
        self.opener = urllib.request.build_opener(RejectRedirect())

    def request(self, path, script=None):
        headers = dict(self.headers)
        body = None
        if script is not None:
            body = urllib.parse.urlencode({"script": script}).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            request = urllib.request.Request(self.url + path, data=body, headers=headers)
            with self.opener.open(request, timeout=30) as response:
                result = response.read(1024 * 1024 + 1)
            if len(result) > 1024 * 1024:
                raise CredentialError("Jenkins 响应超出大小限制")
            return result.decode("utf-8")
        except urllib.error.HTTPError as error:
            raise CredentialError("Jenkins HTTP {}；响应已隐藏，禁止盲目重试写请求".format(error.code)) from None
        except (OSError, ValueError, UnicodeError, TypeError):
            raise CredentialError("Jenkins 请求失败；响应已隐藏，写入结果未知时请重新运行只读核对") from None

    def assert_idle(self):
        try:
            queue = json.loads(self.request("/queue/api/json?tree=items[id]"))["items"]
            computers = json.loads(self.request(
                "/computer/api/json?tree=computer[executors[currentExecutable[url]],oneOffExecutors[currentExecutable[url]]]"))
            entries = computers["computer"]
            busy = any(executor.get("currentExecutable") is not None
                       for computer in entries
                       for executor in computer["executors"] + computer["oneOffExecutors"])
            if not isinstance(queue, list) or not isinstance(entries, list):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError):
            raise CredentialError("无法确认 Jenkins Controller 空闲") from None
        if queue or busy:
            raise CredentialError("Jenkins 存在 running build 或 queue，拒绝创建凭据")


GROOVY = r'''
import jenkins.model.Jenkins
import groovy.json.JsonSlurper
import groovy.json.JsonOutput
import com.cloudbees.plugins.credentials.CredentialsProvider
import com.cloudbees.hudson.plugins.folder.Folder
import com.cloudbees.plugins.credentials.CredentialsScope
import com.cloudbees.plugins.credentials.domains.Domain
import com.cloudbees.plugins.credentials.SecretBytes
import org.jenkinsci.plugins.plaincredentials.impl.FileCredentialsImpl
import org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl
import hudson.util.Secret
import java.security.MessageDigest
try {
    def jenkins = Jenkins.get()
    jenkins.checkPermission(Jenkins.ADMINISTER)
    def input = new JsonSlurper().parseText(new String('__PAYLOAD__'.decodeBase64(), 'UTF-8'))
    def provider = jenkins.getItemByFullName('mobile-apps', Folder.class)
    if (provider == null) { throw new IllegalStateException('mobile-apps-folder-missing') }
    def stores = CredentialsProvider.lookupStores(provider).findAll { it.context == provider }
    if (stores.size() != 1) { throw new IllegalStateException('mobile-apps-store-missing') }
    def store = stores[0]
    def report = []
    synchronized (provider) {
        // Folder 才是凭据边界；同 ID 的 root 或非默认 Domain 项必须先完成迁移。
        def ids = input.items.collect { it.id }
        def shadow = CredentialsProvider.lookupStores(jenkins).findAll { it.context == jenkins }.any { rootStore ->
            rootStore.domains.any { domain -> rootStore.getCredentials(domain).any { it.id in ids } }
        } || store.domains.findAll { it != Domain.global() }.any { domain -> store.getCredentials(domain).any { it.id in ids } }
        if (shadow) { throw new IllegalStateException('signing-credential-shadow') }
        def existing = store.getCredentials(Domain.global())
        input.items.each { item ->
            def matches = existing.findAll { it.id == item.id }
            def status = 'missing'
            if (matches.size() > 1) {
                status = 'conflict'
            } else if (matches.size() == 1) {
                def credential = matches[0]
                def rightType = item.type == 'Secret file' ? credential instanceof FileCredentialsImpl : credential instanceof StringCredentialsImpl
                if (!rightType || credential.scope != CredentialsScope.GLOBAL) {
                    status = 'wrong-type'
                } else if (!item.sha256) {
                    status = 'present-unverified'
                } else {
                    byte[] bytes
                    if (item.type == 'Secret file') {
                        def stream = credential.content
                        try { bytes = stream.bytes } finally { stream.close() }
                    } else {
                        bytes = credential.secret.plainText.getBytes('UTF-8')
                    }
                    def digest = MessageDigest.getInstance('SHA-256').digest(bytes).encodeHex().toString()
                    status = digest == item.sha256 ? 'matching' : 'conflict'
                }
            }
            report << [id: item.id, type: item.type, status: status]
        }
        if (input.apply && !report.any { it.status in ['conflict', 'wrong-type', 'present-unverified'] }) {
            def busy = jenkins.computers.any { computer ->
                (computer.executors + computer.oneOffExecutors).any { it.currentExecutable != null }
            }
            if (busy || jenkins.queue.items.length != 0) {
                println('IOS_CREDENTIAL_REPORT:' + JsonOutput.toJson([error: 'busy']))
                return
            }
            for (int index = 0; index < input.items.size(); index++) {
                def item = input.items[index]
                if (report[index].status == 'missing') {
                    byte[] bytes = item.value.decodeBase64()
                    def credential = item.type == 'Secret file' ?
                        new FileCredentialsImpl(CredentialsScope.GLOBAL, item.id, item.description, item.filename, SecretBytes.fromBytes(bytes)) :
                        new StringCredentialsImpl(CredentialsScope.GLOBAL, item.id, item.description, Secret.fromString(new String(bytes, 'UTF-8')))
                    if (!store.addCredentials(Domain.global(), credential)) {
                        throw new IllegalStateException('create-failed')
                    }
                    report[index].status = 'created'
                }
            }
        }
    }
    println('IOS_CREDENTIAL_REPORT:' + JsonOutput.toJson([credentials: report]))
} catch (Exception ignored) {
    println('IOS_CREDENTIAL_REPORT:' + JsonOutput.toJson([error: 'script-failed']))
}
null
'''


def reconcile(client, items, apply=False):
    if apply:
        if not items or any("value" not in item for item in items):
            raise CredentialError("--apply 必须提供本地凭据材料")
        client.assert_idle()
    payload_items = [dict(item) for item in items]
    if not apply:
        for item in payload_items:
            item.pop("value", None)
    payload = base64.b64encode(json.dumps({"items": payload_items, "apply": apply}).encode()).decode()
    source = client.request("/scriptText", GROOVY.replace("__PAYLOAD__", payload))
    try:
        reports = [line[len(MARKER):] for line in source.splitlines() if line.startswith(MARKER)]
        if len(reports) != 1:
            raise ValueError()
        response = json.loads(reports[0])
        if response.get("error"):
            raise CredentialError("Jenkins 凭据核对或创建失败；请重新运行只读核对")
        rows = response["credentials"]
        allowed = {"matching", "missing", "present-unverified", "wrong-type", "conflict"}
        if apply:
            allowed.add("created")
        if len(rows) != len(items):
            raise ValueError()
        for row, item in zip(rows, items):
            if (set(row) != {"id", "type", "status"} or row["id"] != item["id"]
                    or row["type"] != item["type"] or row["status"] not in allowed):
                raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise CredentialError("Jenkins 凭据响应无效；原始响应已隐藏，请重新运行只读核对") from None
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--brand", required=True)
    parser.add_argument("--jenkins-url", default=os.environ.get("JENKINS_URL", ""))
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--auth-file", type=Path)
    auth.add_argument("--headers-command", help="受信任 helper 命令，以 argv 执行，不使用 shell")
    parser.add_argument("--p12", type=Path)
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--api-key-config", type=Path, help="0600 JSON：keyPath、keyId、issuerId、teamId")
    parser.add_argument("--apply", action="store_true", help="显式创建缺失项；已有冲突绝不覆盖")
    args = parser.parse_args(argv)
    try:
        items = credential_inputs(args.config, args.brand, args.p12, args.password_file, args.api_key_config)
        client = JenkinsClient(args.jenkins_url, auth_headers(args.auth_file, args.headers_command))
        if args.apply and any("value" not in item for item in items):
            raise CredentialError("--apply 必须提供本地凭据材料")
        rows = reconcile(client, items)
        if (args.apply and any(row["status"] == "missing" for row in rows)
                and not any(row["status"] in ("conflict", "wrong-type") for row in rows)):
            rows = reconcile(client, items, apply=True)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return int(any(row["status"] in ("conflict", "wrong-type") for row in rows))
    except CredentialError as error:
        print("iOS Jenkins 凭据接入失败：{}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
