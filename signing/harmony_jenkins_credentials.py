#!/usr/bin/env python3
"""接入已确认品牌的 HarmonyOS Jenkins 凭据；默认核对，--apply 只创建缺失项。"""

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from project import SOURCE_ROOT

from signing import harmony_provisioning as signing
from signing.ios_jenkins_credentials import CredentialError, JenkinsClient, auth_headers, private_bytes, reconcile


DEFAULT_CONFIG = SOURCE_ROOT / "ci/brands.json"

REFRESH_MARKER = "HARMONY_PROFILE_REPORT:"
REFRESH_GROOVY = r'''
import jenkins.model.Jenkins
import groovy.json.JsonSlurper
import groovy.json.JsonOutput
import com.cloudbees.plugins.credentials.CredentialsProvider
import com.cloudbees.hudson.plugins.folder.Folder
import com.cloudbees.plugins.credentials.CredentialsScope
import com.cloudbees.plugins.credentials.domains.Domain
import com.cloudbees.plugins.credentials.SecretBytes
import org.jenkinsci.plugins.plaincredentials.impl.FileCredentialsImpl
import java.security.MessageDigest
try {
    def jenkins = Jenkins.get()
    jenkins.checkPermission(Jenkins.ADMINISTER)
    def input = new JsonSlurper().parseText(new String('__PAYLOAD__'.decodeBase64(), 'UTF-8'))
    def allowed = [input.namespace + '-' + input.brand + '-designated-release-profile',
                   input.namespace + '-' + input.brand + '-devices-json']
    if (input.items.size() != 2 || input.items.collect { it.id } != allowed ||
        input.items.any { it.type != 'Secret file' }) throw new IllegalStateException('scope')
    def provider = jenkins.getItemByFullName('mobile-apps', Folder.class)
    if (provider == null) { throw new IllegalStateException('mobile-apps-folder-missing') }
    def stores = CredentialsProvider.lookupStores(provider).findAll { it.context == provider }
    if (stores.size() != 1) { throw new IllegalStateException('mobile-apps-store-missing') }
    def store = stores[0]
    def digest = { credential ->
        def stream = credential.content
        try { MessageDigest.getInstance('SHA-256').digest(stream.bytes).encodeHex().toString() }
        finally { stream.close() }
    }
    def report = []
    synchronized (provider) {
        // Folder 才是凭据边界；同 ID 的 root 或非默认 Domain 项必须先完成迁移。
        def ids = input.items.collect { it.id }
        def shadow = CredentialsProvider.lookupStores(jenkins).findAll { it.context == jenkins }.any { rootStore ->
            rootStore.domains.any { domain -> rootStore.getCredentials(domain).any { it.id in ids } }
        } || store.domains.findAll { it != Domain.global() }.any { domain -> store.getCredentials(domain).any { it.id in ids } }
        if (shadow) { throw new IllegalStateException('signing-credential-shadow') }
        def old = []
        input.items.each { item ->
            def matches = store.getCredentials(Domain.global()).findAll { it.id == item.id }
            def value = matches.size() == 1 ? matches[0] : null
            def right = value instanceof FileCredentialsImpl && value.scope == CredentialsScope.GLOBAL
            def current = right ? digest(value) : null
            def status = matches.size() > 1 ? 'conflict' : value && !right ? 'wrong-type' : !value ? 'missing' :
                         current == item.sha256 ? 'matching' : 'update-needed'
            report << [id: item.id, type: item.type, status: status, currentSha256: current]
            old << value
        }
        if (input.apply) {
            if (report.any { it.status in ['conflict', 'wrong-type'] } ||
                report.withIndex().any { row, i -> row.currentSha256 != input.items[i].expectedSha256 }) {
                println('HARMONY_PROFILE_REPORT:' + JsonOutput.toJson([error: 'cas-conflict']))
                return
            }
            if (jenkins.queue.items.length != 0 || jenkins.computers.any { computer ->
                (computer.executors + computer.oneOffExecutors).any { it.currentExecutable != null }
            }) {
                println('HARMONY_PROFILE_REPORT:' + JsonOutput.toJson([error: 'busy']))
                return
            }
            def changed = []
            try {
                input.items.eachWithIndex { item, i ->
                    if (report[i].status != 'matching') {
                        byte[] bytes = item.value.decodeBase64()
                        if (MessageDigest.getInstance('SHA-256').digest(bytes).encodeHex().toString() != item.sha256)
                            throw new IllegalStateException('payload')
                        def replacement = new FileCredentialsImpl(CredentialsScope.GLOBAL, item.id,
                            item.description, item.filename, SecretBytes.fromBytes(bytes))
                        def ok = old[i] ? store.updateCredentials(Domain.global(), old[i], replacement) :
                                          store.addCredentials(Domain.global(), replacement)
                        if (!ok) throw new IllegalStateException('update-failed')
                        changed << [index: i, value: replacement]
                    }
                }
                input.items.eachWithIndex { item, i ->
                    def matches = store.getCredentials(Domain.global()).findAll { it.id == item.id }
                    if (matches.size() != 1 || digest(matches[0]) != item.sha256) throw new IllegalStateException('verify')
                    report[i].status = changed.any { it.index == i } ? 'updated' : 'matching'
                    report[i].currentSha256 = item.sha256
                }
            } catch (Exception ignored) {
                // 只回滚本次替换且仍匹配新 SHA 的项；外部竞争写入绝不覆盖。
                changed.reverseEach { change ->
                    try {
                        def matches = store.getCredentials(Domain.global()).findAll { it.id == input.items[change.index].id }
                        if (matches.size() == 1 && digest(matches[0]) == input.items[change.index].sha256) {
                            if (old[change.index]) store.updateCredentials(Domain.global(), matches[0], old[change.index])
                            else store.removeCredentials(Domain.global(), matches[0])
                        }
                    } catch (Exception rollbackIgnored) { }
                }
                println('HARMONY_PROFILE_REPORT:' + JsonOutput.toJson([error: 'refresh-failed']))
                return
            }
        }
    }
    println('HARMONY_PROFILE_REPORT:' + JsonOutput.toJson([credentials: report]))
} catch (Exception ignored) {
    println('HARMONY_PROFILE_REPORT:' + JsonOutput.toJson([error: 'script-failed']))
}
null
'''


def profile_items(items, namespace, brand):
    allowed = [namespace + '-' + brand + suffix for suffix in ('-designated-release-profile', '-devices-json')]
    selected = [item for identity in allowed for item in items if item['id'] == identity]
    if len(selected) != 2 or any(item['type'] != 'Secret file' or 'value' not in item for item in selected):
        raise CredentialError('Profile 维护必须提供当前品牌的完整已校验材料')
    return selected


def refresh_profiles(client, items, namespace, brand, expected=None):
    selected = profile_items(items, namespace, brand)
    apply = expected is not None
    payload_items = [dict(item) for item in selected]
    if apply:
        if set(expected) != {item['id'] for item in selected} or any(
                value is not None and (not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value))
                for value in expected.values()):
            raise CredentialError('Profile CAS 必须提供两项先前 SHA，null 仅表示缺项')
        client.assert_idle()
        for item in payload_items:
            item['expectedSha256'] = expected[item['id']]
    else:
        for item in payload_items:
            item.pop('value')
    payload = base64.b64encode(json.dumps({'refreshProfile': True, 'namespace': namespace, 'brand': brand,
                                         'items': payload_items, 'apply': apply}).encode()).decode()
    source = client.request('/scriptText', REFRESH_GROOVY.replace('__PAYLOAD__', payload))
    try:
        reports = [line[len(REFRESH_MARKER):] for line in source.splitlines() if line.startswith(REFRESH_MARKER)]
        if len(reports) != 1:
            raise ValueError()
        response = json.loads(reports[0])
        if response.get('error'):
            raise CredentialError('Profile CAS 更新未完成；必须重新只读核对，禁止后续打包和盲目重试')
        rows = response['credentials']
        allowed = {'matching', 'update-needed', 'missing', 'wrong-type', 'conflict'} | ({'updated'} if apply else set())
        if len(rows) != 2:
            raise ValueError()
        for row, item in zip(rows, selected):
            if (set(row) != {'id', 'type', 'status', 'currentSha256'} or row['id'] != item['id']
                    or row['type'] != item['type'] or row['status'] not in allowed
                    or (row['currentSha256'] is not None and (not isinstance(row['currentSha256'], str)
                        or not re.fullmatch(r'[0-9a-f]{64}', row['currentSha256'])))
                    or (row['status'] in {'matching', 'update-needed', 'updated'} and row['currentSha256'] is None)):
                raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise CredentialError('Profile CAS 响应未知；原始内容已隐藏，必须重新只读核对') from None
    return rows


def export_maintenance_payload(items, config, path, expected):
    selected = profile_items(items, config['credentialNamespace'], config['brand'])
    p12_id = config['credentialNamespace'] + '-release-p12'
    p12 = [item for item in items if item['id'] == p12_id]
    if len(p12) != 1 or 'value' not in p12[0]:
        raise CredentialError('维护 payload 缺少已校验 P12')
    allowed = {item['id'] for item in selected + p12}
    if (not isinstance(expected, dict) or set(expected) != allowed
            or any(value is not None and (not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value))
                   for value in expected.values())):
        raise CredentialError('维护 payload 必须提供三项精确的旧 SHA；null 表示已确认缺项')
    payload = {'namespace': config['credentialNamespace'], 'brand': config['brand'],
               'items': p12 + selected, 'expected': expected}
    signing.private_dir(Path(path).parent)
    signing.save(path, json.dumps(payload, sort_keys=True).encode())
    return [item['id'] for item in payload['items']]


def credential_inputs(config_path, brand, service_account=None, materials_dir=None, password_file=None):
    config = signing.load_brand(config_path, brand)
    namespace = config["credentialNamespace"]
    root = signing.read_json(Path(config_path).read_bytes())
    if (not re.fullmatch(r"[a-z][a-z0-9-]*", namespace)
            or any(key != config["teamKey"] and isinstance(value, dict) and value.get("credentialNamespace") == namespace
                   for key, value in root["_harmonyTeams"].items())):
        raise CredentialError("HarmonyOS Team 凭据命名空间无效或重复")
    known = {"sa-key": ("Secret file", "service-account.pem"), "sa-key-id": ("Secret text", None),
             "sa-sub-account": ("Secret text", None), "release-p12-password": ("Secret text", None),
             "release-p12": ("Secret file", "release.p12"),
             brand + "-designated-release-profile": ("Secret file", "designated-release.p7b"),
             brand + "-devices-json": ("Secret file", "devices.json")}
    descriptions = {
        "sa-key": "Service Account 私钥 | 签名维护消费；管理员预置",
        "sa-key-id": "Service Account Key ID | 签名维护消费；与 SA 私钥配套",
        "sa-sub-account": "Service Account 子账号 | 签名维护消费；与 SA 私钥配套",
        "release-p12-password": "P12 密码 | 鸿蒙打包、签名维护消费；与 P12 配套",
        "release-p12": "发布证书与私钥 | 鸿蒙打包、签名维护消费；同 Team 复用",
        brand + "-designated-release-profile": "品牌 {} | 指定设备 Profile | 鸿蒙打包消费；签名维护按应用同步".format(brand),
        brand + "-devices-json": "品牌 {} | Profile 设备清单 | 鸿蒙验签消费；与 Profile 成组同步".format(brand),
    }
    values = {}
    if service_account:
        account = signing.read_json(private_bytes(service_account, "Service Account JSON"))
        if not isinstance(account, dict) or any(not isinstance(account.get(field), str) or not account[field].strip()
                                             for field in ("private_key", "key_id", "sub_account")):
            raise CredentialError("Service Account 缺少官方 private_key、key_id、sub_account 字段")
        key = account["private_key"].encode()
        signing.openssl("rsa", "-check", "-noout", data=key)
        public = signing.openssl("rsa", "-pubout", data=key)
        size = re.search(r"Public-Key:\s*\((\d+) bit\)", signing.openssl("pkey", "-pubin", "-text", "-noout", data=public).decode())
        if not size or int(size[1]) < 2048:
            raise CredentialError("Service Account 必须为至少 2048 bit 的 RSA 私钥")
        values.update({"sa-key": key, "sa-key-id": account["key_id"].encode(),
                       "sa-sub-account": account["sub_account"].encode()})
    password = None
    if password_file:
        password = private_bytes(password_file, "P12 密码").decode("utf-8").rstrip("\r\n")
        if not password.strip() or any(char in password for char in "\r\n\0"):
            raise CredentialError("P12 密码必须为非空单行文本")
        values["release-p12-password"] = password.encode()
    if materials_dir:
        directory = Path(materials_dir)
        info = directory.stat()
        if directory.is_symlink() or not directory.is_dir() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise CredentialError("签名材料目录必须属于当前用户且为 0700 非符号链接目录")
        if password is None:
            raise CredentialError("--materials-dir 必须同时提供 --password-file")
        material = {name: private_bytes(directory / name, name) for name in
                    ("release.p12", "release.cer", "designated-release.p7b", "devices.json")}
        cert = signing.openssl("pkcs12", "-in", directory / "release.p12", "-clcerts", "-nokeys",
                               "-passin", "stdin", data=password.encode())
        key = signing.openssl("pkcs12", "-in", directory / "release.p12", "-nocerts", "-nodes",
                              "-passin", "stdin", data=password.encode())
        if cert.count(b"-----BEGIN CERTIFICATE-----") != 1 or key.count(b"PRIVATE KEY-----") != 2:
            raise CredentialError("P12 必须含唯一发布证书和私钥")
        certificate = signing.certificate_pem(material["release.cer"])
        if (signing.public_key(key) != signing.certificate_public_key(certificate)
                or signing.openssl("x509", "-outform", "DER", data=signing.certificate_pem(cert)) != material["release.cer"]):
            raise CredentialError("P12 私钥、发布证书与 release.cer 不匹配")
        rows = signing.read_json(material["devices.json"])
        if not isinstance(rows, list) or not rows:
            raise CredentialError("Profile 设备 roster 必须为非空数组")
        identities = signing.device_union(rows, [])
        if (len(identities) != len(rows) or any(not isinstance(row.get("udid"), str) for row in rows)
                or len({row["udid"] for row in rows}) != len(rows)):
            raise CredentialError("Profile roster 的 AGC ID 与 UDID 必须明确且唯一")
        signing.validate_profile(material["designated-release.p7b"], config, certificate, rows)
        if any(private_bytes(directory / name, name) != content for name, content in material.items()):
            raise CredentialError("签名材料校验期间文件发生变化")
        values.update({"release-p12": material["release.p12"],
                       brand + "-designated-release-profile": material["designated-release.p7b"],
                       brand + "-devices-json": material["devices.json"]})
    items = []
    for suffix, (kind, filename) in known.items():
        if values and suffix not in values:
            continue
        item = {"id": namespace + "-" + suffix, "type": kind,
                "description": "HarmonyOS | Team {} | {}".format(config["teamKey"], descriptions[suffix])}
        if suffix in values:
            value = values[suffix]
            item.update(value=base64.b64encode(value).decode(), sha256=hashlib.sha256(value).hexdigest(), filename=filename)
        items.append(item)
    return items


def expected_snapshot(path, config):
    value = signing.read_json(private_bytes(path, '维护凭据旧 SHA snapshot'))
    if (not isinstance(value, dict) or set(value) != {'namespace', 'brand', 'items'}
            or value['namespace'] != config['credentialNamespace'] or value['brand'] != config['brand']
            or not isinstance(value['items'], list) or len(value['items']) != 3):
        raise CredentialError('维护 snapshot 品牌、命名空间或结构不匹配')
    expected = {}
    for item in value['items']:
        if not isinstance(item, dict) or set(item) != {'id', 'status', 'sha256'} or not isinstance(item['id'], str):
            raise CredentialError('维护 snapshot 项结构未知')
        digest = item['sha256']
        if (item['id'] in expected or (item['status'] == 'missing' and digest is not None)
                or (item['status'] == 'present' and (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)))
                or item['status'] not in ('missing', 'present')):
            raise CredentialError('维护 snapshot 旧 SHA 或状态无效')
        expected[item['id']] = digest
    return expected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--brand", required=True)
    parser.add_argument("--jenkins-url", default=os.environ.get("JENKINS_URL", ""))
    parser.add_argument("--headers-command", help="受信任 Jenkins 认证 helper，不使用 shell")
    parser.add_argument("--service-account-json", type=Path)
    parser.add_argument("--materials-dir", type=Path)
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--refresh-profile", action="store_true", help="仅管理员维护用途：成组 CAS 更新当前品牌 Profile/roster")
    parser.add_argument("--export-maintenance-payload", type=Path, help="导出 0600 受控 payload；不访问 Jenkins")
    parser.add_argument("--expected-old-json", "--expected-file", type=Path, help="0600 Controller 只读 snapshot：namespace、brand、items[{id,status,sha256}]")
    args = parser.parse_args(argv)
    try:
        items = credential_inputs(args.config, args.brand, args.service_account_json, args.materials_dir, args.password_file)
        config = signing.load_brand(args.config, args.brand)
        if args.export_maintenance_payload:
            if args.apply or args.refresh_profile or not args.expected_old_json:
                raise CredentialError("导出 payload 不可同时 --apply/--refresh-profile，必须提供先前 SHA JSON")
            ids = export_maintenance_payload(items, config, args.export_maintenance_payload,
                                             expected_snapshot(args.expected_old_json, config))
            print(json.dumps({"exported": True, "credentialIds": ids}, ensure_ascii=False))
            return 0
        client = JenkinsClient(args.jenkins_url, auth_headers(headers_command=args.headers_command))
        if args.apply and any("value" not in item for item in items):
            raise CredentialError("--apply 必须提供已校验的本地材料")
        # 复用既有通用 SecretFile/SecretText 后端；其内部 iOS 标记与描述不影响凭据 schema。
        if args.refresh_profile:
            selected = profile_items(items, config['credentialNamespace'], args.brand)
            immutable = [item for item in items if item not in selected]
            rows = reconcile(client, immutable) if immutable else []
            changed = refresh_profiles(client, selected, config['credentialNamespace'], args.brand)
            if any(row['status'] in ('conflict', 'wrong-type', 'present-unverified') for row in rows + changed):
                print(json.dumps(rows + changed, ensure_ascii=False, indent=2))
                return 1
            if args.apply:
                if any(row['status'] == 'missing' for row in rows):
                    rows = reconcile(client, immutable, apply=True)
                if any(row['status'] != 'matching' for row in changed):
                    changed = refresh_profiles(client, selected, config['credentialNamespace'], args.brand,
                                               {row['id']: row['currentSha256'] for row in changed})
                if any(row['status'] not in ('matching', 'created', 'updated') for row in rows + changed):
                    raise CredentialError('Profile 维护未完整成功，禁止继续打包')
            print(json.dumps(rows + changed, ensure_ascii=False, indent=2))
            return 0
        rows = reconcile(client, items)
        if (args.apply and any(row["status"] == "missing" for row in rows)
                and not any(row["status"] in ("conflict", "wrong-type", "present-unverified") for row in rows)):
            rows = reconcile(client, items, apply=True)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return int(any(row["status"] in ("conflict", "wrong-type") for row in rows))
    except (CredentialError, signing.ProvisioningError, OSError, UnicodeError):
        error = sys.exc_info()[1]
        message = str(error) if isinstance(error, (CredentialError, signing.ProvisioningError)) else "受控材料读取失败"
        print("HarmonyOS Jenkins 凭据接入失败：" + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
