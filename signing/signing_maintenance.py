#!/usr/bin/env python3
"""统一签名维护：解析品牌/Team，复用平台 CLI；秘密材料留在节点受限目录。"""
import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from project import SOURCE_ROOT, TOOL_ROOT

from signing import ios_signing_setup as ios, harmony_provisioning as harmony, harmony_jenkins_credentials as credentials, ios_jenkins_credentials as ios_credentials, signing_devices as device_catalog

ROOT = SOURCE_ROOT
SAFE_KEY = re.compile(r"[a-z][a-z0-9_-]*")


def ios_channels(config, brand, operation):
    if operation != "onboard":
        return ["pgyer"]
    root = json.loads(Path(config).read_text(encoding="utf-8"))
    names = root.get(brand, {}).get("ios", {}).get("signing", {}).get("test", {}).get("profileNames", {})
    channels = [channel for channel in ("pgyer", "testflight") if names.get(channel)]
    if not channels:
        raise ValueError("iOS 品牌未配置签名渠道")
    return channels


def requested_devices(args, brand=None):
    source = getattr(args, "device_source", None)
    ids = getattr(args, "device_ids", None)
    source = os.environ.get("DEVICE_SOURCE", "manual") if source is None else source
    ids = os.environ.get("DEVICE_IDS", "all") if ids is None else ids
    name, udid = os.environ.get("DEVICE_NAME", ""), os.environ.get("DEVICE_UDID", "")
    if bool(name) != bool(udid):
        raise device_catalog.DeviceError("设备输入必须同时填写 DEVICE_NAME 和 DEVICE_UDID")
    if source not in ("shared", "manual"):
        raise device_catalog.DeviceError("DEVICE_SOURCE 必须为 shared 或 manual")
    if args.platform == "ios" and args.operation == "onboard" and "pgyer" not in ios_channels(args.config, brand or args.brand, args.operation):
        if name or udid or ids != "all":
            raise device_catalog.DeviceError("所选品牌仅使用 TestFlight，不使用设备；请清空手动设备输入和特定 DEVICE_IDS")
        return []
    selected = device_catalog.select_devices(args.state_root, args.platform, source, ids,
                                             name, udid)
    if args.operation == "devices" and not selected:
        raise device_catalog.DeviceError("设备同步必须选择批准设备或填写 DEVICE_NAME 和 DEVICE_UDID")
    return selected


def resolve(config, platform, operation, brands):
    root = json.loads(Path(config).read_text(encoding="utf-8"))
    selected = [key for key, value in root.items() if not key.startswith("_") and
                isinstance(value, dict) and platform in value] if brands == "all" else brands.split(",")
    rows, seen, teams = [], set(), set()
    for value in selected:
        brand = value.strip()
        if not SAFE_KEY.fullmatch(brand) or brand not in root:
            raise ValueError("未知或无效品牌")
        if platform == "ios":
            item = ios.load_brand(config, brand, ios_channels(config, brand, operation))
            team_key = item["team"]
            team = root["_iosTeams"][team_key]
            if operation == "devices" and not item["enabled"]:
                raise ValueError("设备同步要求品牌已启用测试签名")
            profile_key = (item["teamId"], item["bundleId"], tuple(item["profileNames"].values()))
        else:
            item = root[brand].get("harmony", {})
            team_key = item.get("teamKey", "")
            team = root.get("_harmonyTeams", {}).get(team_key, {})
            signing = item.get("signing", {})
            if not all(item.get(key) for key in ("appId", "bundleName")) or not all(
                    signing.get(key) for key in ("certName", "profileName")):
                raise ValueError("Harmony App/Bundle/签名配置未确认")
            profile_key = (team_key, item["bundleName"], signing["profileName"])
        namespace = team.get("credentialNamespace", "")
        if not SAFE_KEY.fullmatch(str(team_key)) or not re.fullmatch(r"[a-z][a-z0-9-]*", namespace):
            raise ValueError("Team 或凭据命名空间未确认")
        if profile_key in seen:
            continue
        seen.add(profile_key)
        rows.append({"brand": brand, "team": team_key, "namespace": namespace,
                     "registerDevice": team_key not in teams,
                     "reuseP12": platform == "ios" and team.get("testSigningSource") == "jenkins"})
        teams.add(team_key)
    if not rows:
        raise ValueError("没有所选平台已确认的品牌")
    return rows


def private_dir(path):
    if path.is_symlink() or not path.is_dir():
        raise ValueError("签名持久目录须由管理员预建，不能是符号链接")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("签名持久目录必须属于当前用户且权限为 0700")


def team_directory(root, platform, team):
    # 节点管理员提供持久路径；禁止把恢复私钥写进可被 deleteDir 清理的 workspace。
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("签名持久目录必须是无符号链接的绝对路径")
    private_dir(root)
    workspace = Path(os.environ.get("WORKSPACE", ROOT)).resolve()
    if any(root == parent or parent in root.parents for parent in (ROOT, TOOL_ROOT, workspace)):
        raise ValueError("签名持久目录不能位于 workspace/仓库中")
    for child in (root / platform, root / platform / team):
        if not child.exists() and not child.is_symlink():
            child.mkdir(mode=0o700)
        private_dir(child)
    return root / platform / team


def sync_directory(team, brand):
    directory = team / "materials" / brand / ".jenkins-maintenance"
    for child in (team / "materials", team / "materials" / brand, directory):
        if not child.exists() and not child.is_symlink():
            child.mkdir(mode=0o700)
        private_dir(child)
    return directory


def invoke(command, env=None):
    result = subprocess.run(command, env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Fastlane 和供应商错误可能包含完整 UDID/请求，统一入口不透传原始输出。
        raise ValueError("平台维护入口失败（退出码 {}），秘密状态已保留；检查受限节点".format(result.returncode))
    return result.stdout


def ios_run(args, row, team, temporary, requested):
    item = ios.load_brand(args.config, args.brand, ios_channels(args.config, args.brand, args.operation))
    env = dict(os.environ, ASC_TEAM_ID=item["teamId"])
    env["ASC_API_KEY_PATH"] = str(temporary / "api.p8")
    shutil.copyfile(os.environ["ASC_API_KEY_PATH"], env["ASC_API_KEY_PATH"])
    os.chmod(env["ASC_API_KEY_PATH"], 0o600)
    p12, password = None, None
    if bool(os.environ.get("IOS_DISTRIBUTION_P12")) != bool(os.environ.get("IOS_DISTRIBUTION_P12_PASSWORD")):
        raise ValueError("绑定 iOS P12/password 缺一项，禁止重建私钥")
    if row["reuseP12"] and os.environ.get("IOS_DISTRIBUTION_P12"):
        p12, password = temporary / "existing.p12", temporary / "password"
        shutil.copyfile(os.environ["IOS_DISTRIBUTION_P12"], p12)
        os.chmod(p12, 0o600)
        password.write_text(os.environ["IOS_DISTRIBUTION_P12_PASSWORD"], encoding="utf-8")
        password.chmod(0o600)
    else:
        source = ROOT / item["signingPath"]
        if (source / "distribution.p12").exists() != (source / "distribution-p12.password").exists():
            raise ValueError("仓库 iOS P12/password 缺一项，禁止重建私钥")
        if (source / "distribution.p12").exists():
            p12, password = temporary / "existing.p12", temporary / "password"
            for origin, target in ((source / "distribution.p12", p12), (source / "distribution-p12.password", password)):
                shutil.copyfile(origin, target)
                target.chmod(0o600)
    if args.operation == "onboard":
        output = team / "materials"
        if (output / "distribution.p12").exists() != (output / "distribution-p12.password").exists():
            raise ValueError("持久 iOS P12/password 缺一项，禁止重建私钥")
        command = [sys.executable, str(TOOL_ROOT / "signing/ios_signing_setup.py"), args.mode,
                   "--config", str(args.config), "--brand", args.brand, "--channels", ",".join(item["profileNames"]),
                   "--state-dir", str(team / "state"), "--output-dir", str(team / "materials")]
        if p12:
            command += ["--p12-path", str(p12), "--password-file", str(password)]
        if not requested:
            report = json.loads(invoke(command, env))
            print(json.dumps(report, ensure_ascii=False))
            return
        if "pgyer" not in item["profileNames"]:
            raise ValueError("选中设备要求配置 pgyer Ad Hoc Profile")
    if not p12:
        p12, password = team / "materials/distribution.p12", team / "materials/distribution-p12.password"
    api_env = {key: os.environ.get(key) for key in ("ASC_API_KEY_PATH", "ASC_TEAM_ID")}
    os.environ.update(ASC_API_KEY_PATH=env["ASC_API_KEY_PATH"], ASC_TEAM_ID=item["teamId"])
    try:
        api = ios.AppleAPI()
    finally:
        for key, value in api_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    state = team / "state"
    for directory in (team / "materials", state):
        if directory.exists():
            private_dir(directory)
    with ios.locked_directories(team / "materials", state) if args.mode == "apply" else contextlib.nullcontext():
        if args.operation == "onboard":
            # 首次证书复用现有接入流程；Ad Hoc 只由明确选择的设备与原 Profile 并集生成。
            certificate_item = dict(item, profileNames={key: value for key, value in item["profileNames"].items() if key != "pgyer"})
            report = ios.setup(certificate_item, api, team / "materials", state, args.mode == "apply",
                               p12_source=p12 if p12.exists() else None,
                               password_source=password if p12.exists() else None)
            print(json.dumps(report, ensure_ascii=False))
            if args.mode == "plan" and (not p12.exists() or report["certificate"] == "create-needed"):
                print(json.dumps({"brand": args.brand, "mode": "plan", "profile": item["profileNames"]["pgyer"],
                                  "selectedDeviceCount": len(requested), "refreshNeeded": True, "certificatePending": True}))
                return
        ios_device_sync(api, args, item, state, p12, password, requested=requested)


def ios_device_sync(api, args, item, state, p12, password, name=None, udid=None, requested=None):
    requested = requested if requested is not None else [{"name": name, "udid": udid}]
    selected = {}
    for row in requested:
        device = device_catalog.normalize_device("ios", row)
        if device["udid"] in selected and selected[device["udid"]] != device:
            raise ValueError("重复 UDID 的设备参数冲突")
        selected[device["udid"]] = device
    if not selected:
        raise ValueError("设备同步必须选择至少一台设备")
    # Team ENV 不足以证明 token 身份，所有外部写入前核验真实证书和 P12。
    _, identity = ios.p12_identity(p12, ios.restricted(password))
    if identity["teamId"] != item["teamId"]:
        raise ValueError("P12 与所选 Team 不一致")
    ios.expiry_warning(identity["expires"], 30, "证书")
    certificates = [(row, ios.remote_certificate(row)) for row in api.all("certificates?limit=200")
                    if row["attributes"]["certificateType"] in ("DISTRIBUTION", "IOS_DISTRIBUTION")]
    if not certificates or any(cert["teamId"] != item["teamId"] for _, cert in certificates):
        raise ValueError("API Key 与品牌 Team 不能确认一致")
    matches = [row for row, cert in certificates if cert["der"] == identity["der"]]
    if len(matches) != 1:
        raise ValueError("P12 证书未在 Team 找到唯一匹配，禁止写入")
    cert_id = matches[0]["id"]
    bundles = [row for row in api.all("bundleIds?" + ios.urllib.parse.urlencode({"filter[identifier]": item["bundleId"], "limit": 200}))
               if row["attributes"]["identifier"] == item["bundleId"] and row["attributes"].get("platform") in ("IOS", "UNIVERSAL")]
    if len(bundles) != 1:
        raise ValueError("目标 Bundle 未在 Team 唯一登记，禁止写入")
    bundle_id, profile_name = bundles[0]["id"], item["profileNames"]["pgyer"]

    def inventory():
        rows = api.all("devices?limit=200")
        enabled, registered = {}, {}
        for row in rows:
            value = row["attributes"].get("udid", "").lower()
            if value in selected:
                if value in registered or row["attributes"].get("status") != "ENABLED" or row["attributes"].get("platform") != "IOS":
                    raise ValueError("设备已登记但未启用或身份冲突，禁止自动注册")
                registered[value] = row
            if row["attributes"].get("status") == "ENABLED" and row["attributes"].get("platform") == "IOS":
                enabled[row["id"]] = row
        return enabled, registered

    def profile():
        rows = [row for row in api.all("profiles?limit=200") if row["attributes"]["name"] == profile_name]
        if len(rows) > 1:
            raise ValueError("多个同名 Profile，须管理员处理")
        return rows[0] if rows else None

    enabled, registered = inventory()
    found = profile()
    marker = state / ("devices-" + hashlib.sha256((item["bundleId"] + ":" + profile_name).encode()).hexdigest() + "-pending.json")
    pending = json.loads(ios.restricted(marker)) if marker.exists() or marker.is_symlink() else None
    if pending and (not isinstance(pending.get("previousDeviceIds"), list) or not isinstance(pending.get("intent"), dict)):
        raise ValueError("旧设备维护 pending 结构不兼容，保留待管理员只读查回")
    previous_ids = (set(pending["previousDeviceIds"]) if pending else
        {row["id"] for row in api.all("profiles/" + ios.urllib.parse.quote(found["id"], safe="") + "/relationships/devices")} if found else set())
    if not previous_ids <= set(enabled):
        raise ValueError("旧 Profile 包含未启用或未知设备，禁止删除")
    wanted_udids = sorted({enabled[value]["attributes"]["udid"].lower() for value in previous_ids} | set(selected))
    intent = {"brand": args.brand, "teamId": item["teamId"], "bundle": bundle_id, "certificate": cert_id,
              "profile": profile_name, "selected": [{"udid": value, "nameSHA256": hashlib.sha256(selected[value]["name"].encode()).hexdigest()}
                                                       for value in sorted(selected)], "udids": wanted_udids}
    device_ids = previous_ids | {row["id"] for row in registered.values()}

    def finish(profile_row, status):
        if args.mode == "apply":
            ios.save(state / (args.brand + "-pgyer.mobileprovision"), base64.b64decode(profile_row["attributes"]["profileContent"], validate=True))
            marker.unlink(missing_ok=True)
            for path in state.glob(marker.stem + "-*.json"):
                path.unlink()
        print(json.dumps({"brand": args.brand, "mode": args.mode, "profile": status, "deviceCount": len(device_ids)}))

    if pending:
        if pending.get("intent") != intent:
            raise ValueError("未知设备维护请求的参数或原设备集合变化，保留 pending，须管理员处理")
        if len(registered) != len(selected) or found is None or found["id"] == pending.get("oldProfileId"):
            raise ValueError("未知写入未查回完整新 Profile/设备，保留 pending；禁止重复删除或创建")
        ios.profile_check(api, found, bundle_id, cert_id, device_ids, ios.PROFILE_TYPES["pgyer"], 30)
        finish(found, "recovered")
        return
    if found:
        ios.profile_check(api, found, bundle_id, cert_id, previous_ids, ios.PROFILE_TYPES["pgyer"], 30)
    refresh = len(registered) != len(selected) or found is None or previous_ids != device_ids
    if args.mode == "plan":
        print(json.dumps({"brand": args.brand, "mode": "plan", "selectedDeviceCount": len(selected),
                          "missingDeviceCount": len(selected) - len(registered), "profile": profile_name, "refreshNeeded": refresh}))
        return
    if not refresh:
        finish(found, "reuse")
        return
    if len(registered) != len(selected) and not args.register_device:
        raise ValueError("设备尚未注册，禁止跨品牌跳过团队注册")
    pending = {"intent": intent, "previousDeviceIds": sorted(previous_ids),
               "oldProfileId": found["id"] if found else None, "phase": "register"}
    ios.save(marker, json.dumps(pending).encode())
    for value, device in selected.items():
        if value not in registered:
            ios.create_once(api, "devices", {"data": {"type": "devices", "attributes": {
                "name": device["name"], "udid": value, "platform": "IOS"}}},
                state / (marker.stem + "-device-" + device_catalog.device_id("ios", value) + ".json"),
                lambda value=value: inventory()[1].get(value))
    enabled, registered = inventory()
    device_ids = previous_ids | {row["id"] for row in registered.values()}
    if (len(registered) != len(selected) or not previous_ids <= set(enabled)
            or sorted({enabled[value]["attributes"]["udid"].lower() for value in device_ids}) != wanted_udids):
        raise ValueError("注册后的完整设备集合不一致，保留 pending")
    if found:
        # 仅删除写入前核验的旧 ID；未知响应保留每阶段已 fsync 的意图。
        pending["phase"] = "delete"
        ios.save(marker, json.dumps(pending).encode())
        api.request("profiles/" + ios.urllib.parse.quote(pending["oldProfileId"], safe=""), method="DELETE")
        if profile() is not None:
            raise ValueError("删除后仍存在同名 Profile，保留 pending，禁止创建")
    pending["phase"] = "create"
    ios.save(marker, json.dumps(pending).encode())

    def recover_profile():
        recovered = profile()
        if recovered:
            if recovered["id"] == pending["oldProfileId"]:
                raise ValueError("旧 Profile 仍存在，禁止覆盖未知结果")
            ios.profile_check(api, recovered, bundle_id, cert_id, device_ids, ios.PROFILE_TYPES["pgyer"], 30)
        return recovered

    relationships = {"bundleId": {"data": {"type": "bundleIds", "id": bundle_id}},
                     "certificates": {"data": [{"type": "certificates", "id": cert_id}]},
                     "devices": {"data": [{"type": "devices", "id": value} for value in sorted(device_ids)]}}
    found = ios.create_once(api, "profiles", {"data": {"type": "profiles", "attributes": {
        "name": profile_name, "profileType": ios.PROFILE_TYPES["pgyer"]}, "relationships": relationships}},
        state / (marker.stem + "-profile.json"), recover_profile)
    ios.profile_check(api, found, bundle_id, cert_id, device_ids, ios.PROFILE_TYPES["pgyer"], 30)
    finish(found, "refreshed")


def run(args):
    if bool(args.payload_file) != bool(args.expected_file):
        raise ValueError("凭据同步必须同时提供 payload 和 Controller 旧 SHA 快照")
    if args.payload_file:
        if args.mode != "apply" or (args.platform == "ios" and args.operation != "onboard"):
            raise ValueError("凭据 payload 只用于 Harmony apply 或 iOS onboard apply")
        ios.restricted(args.expected_file)
    requested = requested_devices(args)
    row = resolve(args.config, args.platform, args.operation, args.brand)[0]
    team = team_directory(args.state_root, args.platform, row["team"])
    if args.payload_file:
        directory = sync_directory(team, args.brand)
        if args.expected_file != directory / "expected.json" or args.payload_file != directory / "payload.json":
            raise ValueError("Controller 同步文件必须位于所选品牌受限材料目录")
    with tempfile.TemporaryDirectory(prefix=".maintenance-", dir=team) as temporary:
        temporary = Path(temporary)
        if args.platform == "ios":
            expected = None
            if args.payload_file:
                snapshot = ios_credentials.private_json(args.expected_file, "Controller 旧值快照")
                names = {row["namespace"] + "-distribution-p12", row["namespace"] + "-distribution-p12-password"}
                if (set(snapshot) != {"namespace", "brand", "items"} or snapshot["namespace"] != row["namespace"]
                        or snapshot["brand"] != args.brand or not isinstance(snapshot["items"], list)
                        or len(snapshot["items"]) != 2):
                    raise ValueError("iOS Controller 快照身份不一致")
                expected = {}
                for item in snapshot["items"]:
                    if (set(item) != {"id", "status", "sha256"} or item["id"] not in names or item["id"] in expected
                            or (item["status"] == "missing" and item["sha256"] is not None)
                            or (item["status"] == "present" and not re.fullmatch(r"[0-9a-f]{64}", str(item["sha256"])))
                            or item["status"] not in ("present", "missing")):
                        raise ValueError("iOS Controller 快照项无效")
                    expected[item["id"]] = item["sha256"]
                if len({item["status"] for item in snapshot["items"]}) != 1:
                    raise ValueError("iOS P12/password 必须同为缺失或完整")
                if any(expected.values()):
                    source = os.environ.get("IOS_DISTRIBUTION_P12")
                    if not source or "IOS_DISTRIBUTION_P12_PASSWORD" not in os.environ:
                        raise ValueError("已有 iOS P12/password 必须绑定复用，禁止供应商写入")
                    # Jenkins FileBinding 为 0400；持久材料仍使用默认 0600 校验。
                    if (hashlib.sha256(ios_credentials.private_bytes(source, "P12", allowed_modes=(0o400, 0o600))).hexdigest() != expected[row["namespace"] + "-distribution-p12"]
                            or hashlib.sha256(os.environ["IOS_DISTRIBUTION_P12_PASSWORD"].encode()).hexdigest() != expected[row["namespace"] + "-distribution-p12-password"]):
                        raise ValueError("iOS 绑定凭据与 Controller 快照不一致")
            ios_run(args, row, team, temporary, requested)
            if args.payload_file:
                items = ios_credentials.credential_inputs(args.config, args.brand,
                    p12=team / "materials/distribution.p12", password_file=team / "materials/distribution-p12.password")
                if any(expected[item["id"]] not in (None, item["sha256"]) for item in items):
                    raise ValueError("已有 iOS 签名材料不同，禁止替换")
                harmony.save(args.payload_file, json.dumps({"namespace": row["namespace"], "brand": args.brand,
                    "items": items, "expected": expected}).encode())
        else:
            materials = team / "materials"
            output = materials / args.brand
            for directory in (materials, output):
                if not directory.exists() and not directory.is_symlink():
                    directory.mkdir(mode=0o700)
                private_dir(directory)
            command = [sys.executable, str(TOOL_ROOT / "signing/harmony_provisioning.py"),
                       args.mode if args.operation == "onboard" else "devices", "--config", str(args.config),
                       "--brand", args.brand, "--state-dir", str(team / "state"), "--output-dir", str(output)]
            if requested:
                devices = temporary / "devices.json"
                devices.write_text(json.dumps([{"deviceName": row["name"], "udid": row["udid"], "deviceType": row["deviceType"]}
                                               for row in requested]), encoding="utf-8")
                devices.chmod(0o600)
                command += ["--devices-file", str(devices)]
            if args.operation == "devices" and args.mode == "apply":
                command += ["--apply"]
            if args.payload_file:
                config = harmony.load_brand(args.config, args.brand)
                expected = credentials.expected_snapshot(args.expected_file, config)
                p12_sha = expected[config["credentialNamespace"] + "-release-p12"]
                if p12_sha is not None:
                    existing = os.environ.get("HARMONY_EXISTING_P12_PATH")
                    state = team / "state"
                    if not existing or not (state / "release.key").is_file() or not (state / "release.p12").is_file():
                        raise ValueError("已有 Jenkins P12 的 Team state 缺失；先恢复原私钥和缓存 P12，禁止重建证书")
                    private_dir(state)
                    for source in (Path(existing), state / "release.p12"):
                        if hashlib.sha256(harmony.restricted(source)).hexdigest() != p12_sha:
                            raise ValueError("绑定或缓存 P12 与 Controller 快照冲突；禁止供应商写入")
                    certificate = harmony.openssl("pkcs12", "-in", existing, "-nokeys", "-passin", "stdin",
                                                  data=os.environ["HARMONY_P12_PASSWORD"].encode())
                    harmony.stable_p12(state, certificate, os.environ["HARMONY_P12_PASSWORD"])
            invoke(command)
            if args.mode == "apply" and args.payload_file:
                password = temporary / "p12-password"
                password.write_text(os.environ["HARMONY_P12_PASSWORD"], encoding="utf-8")
                password.chmod(0o600)
                invoke([sys.executable, str(TOOL_ROOT / "signing/harmony_jenkins_credentials.py"), "--config", str(args.config),
                        "--brand", args.brand, "--materials-dir", str(output), "--password-file", str(password),
                        "--export-maintenance-payload", str(args.payload_file), "--expected-old-json", str(args.expected_file)])
            print(json.dumps({"brand": args.brand, "mode": args.mode, "operation": args.operation}))
        if args.operation == "devices" and args.mode == "apply" and requested:
            profile = (ios.load_brand(args.config, args.brand, ["pgyer"])["profileNames"]["pgyer"] if args.platform == "ios"
                       else harmony.load_brand(args.config, args.brand)["profileName"])
            device_catalog.merge_catalog(args.state_root, args.platform, requested,
                                         {"brand": args.brand, "team": row["team"], "profile": profile})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "prepare", "run"))
    parser.add_argument("--config", type=Path, default=ROOT / "ci/brands.json")
    parser.add_argument("--platform", choices=("ios", "harmony"), required=True)
    parser.add_argument("--operation", choices=("onboard", "devices"), required=True)
    parser.add_argument("--brands", default="all")
    parser.add_argument("--brand")
    parser.add_argument("--mode", choices=("plan", "apply"), default="plan")
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--register-device", action="store_true")
    parser.add_argument("--device-source", choices=("shared", "manual"))
    parser.add_argument("--device-ids")
    parser.add_argument("--payload-file", type=Path)
    parser.add_argument("--expected-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            rows = resolve(args.config, args.platform, args.operation, args.brands)
            if args.state_root:
                for row in rows:
                    requested_devices(args, row["brand"])
                    team_directory(args.state_root, args.platform, row["team"])
            print(json.dumps(rows, ensure_ascii=False))
        else:
            if not args.brand or not args.state_root:
                raise ValueError("run 必须指定品牌和管理员预建的 state-root")
            if args.command == "prepare":
                row = resolve(args.config, args.platform, args.operation, args.brand)[0]
                directory = sync_directory(team_directory(args.state_root, args.platform, row["team"]), args.brand)
                expected = directory / "expected.json"
                if expected.is_symlink():
                    raise ValueError("旧值快照不能是符号链接")
                expected.touch(mode=0o600, exist_ok=True)
                expected.chmod(0o600)
            else:
                run(args)
        return 0
    except device_catalog.DeviceError as error:
        print("签名维护失败：" + str(error), file=sys.stderr)
        return 1
    except (ValueError, KeyError, TypeError, OSError, ios.SigningError, harmony.ProvisioningError, credentials.CredentialError, ios_credentials.CredentialError):
        print("签名维护失败：参数、持久目录或平台入口无效；未输出原始 Secret/UDID", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
