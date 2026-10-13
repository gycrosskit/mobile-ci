#!/usr/bin/env python3
"""将已加密的签名快照追加到固定私有备份分支，不读取明文或调用者 Git 索引。"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_config import load_project

DEFAULT_PROJECT = load_project()
REMOTE = DEFAULT_PROJECT['repositories'][DEFAULT_PROJECT['signingBackup']['repository']]['ssh']
BRANCH = DEFAULT_PROJECT['signingBackup']['branch']
REF = "refs/heads/" + BRANCH
FETCHED = "refs/remotes/origin/" + BRANCH
LIMIT = 128 * 1024 * 1024
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
SNAPSHOT = re.compile(r"snapshots/\d{8}T\d{12}Z-[0-9a-f]{12}\.enc\.json\Z")
RECOVERY = re.compile(r"recipients/[0-9a-f]{64}\.enc\.json\Z")
INDEX_KEYS = {"format", "snapshot", "contentSha256", "ciphertextSha256", "sourceCommit",
              "buildNumber", "createdAt", "recipientSha256", "recipientRecovery"}


def parse_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON 包含重复字段")
            result[key] = value
        return result
    if not raw or len(raw) > LIMIT:
        raise ValueError("快照为空或过大")
    return json.loads(raw, object_pairs_hook=unique)


def encrypted_snapshot(raw, recovery=False):
    value = parse_json(raw)
    keys = {"format", "nonce", "tag", "ciphertext"}
    if not recovery:
        keys |= {"keyAlgorithm", "recipientSha256", "wrappedKey"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("加密快照字段无效")
    if value["format"] != ("sxmqlive-signing-backup-v1" if recovery else "sxmqlive-signing-backup-v2"):
        raise ValueError("加密快照格式无效")
    if not recovery and value["keyAlgorithm"] != "RSA-OAEP-SHA256":
        raise ValueError("加密快照算法无效")
    if not recovery and (not isinstance(value["recipientSha256"], str) or
                         not HEX64.fullmatch(value["recipientSha256"])):
        raise ValueError("恢复公钥摘要无效")
    fields = [("nonce", {12}), ("tag", {16}), ("ciphertext", None)]
    if not recovery:
        fields.append(("wrappedKey", {384}))
    content_limit = 16 * 1024 if recovery else 64 * 1024 * 1024
    if recovery and len(raw) > 64 * 1024:
        raise ValueError("恢复密文文件过大")
    for key, sizes in fields:
        encoded = value[key]
        if not isinstance(encoded, str):
            raise ValueError("密文字段无效")
        maximum = max(sizes) if sizes is not None else content_limit
        if len(encoded) > ((maximum + 2) // 3) * 4:
            raise ValueError("密文字段过大")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("密文字段无效") from error
        if not decoded or len(decoded) > content_limit or (sizes is not None and len(decoded) not in sizes):
            raise ValueError("密文字段长度无效")
        if base64.b64encode(decoded).decode("ascii") != encoded:
            raise ValueError("密文字段编码无效")
    return value


def validate_index(value, project=None):
    policy_keys = {'projectId', 'scope'}
    if not isinstance(value, dict) or set(value) not in (INDEX_KEYS, INDEX_KEYS | policy_keys):
        raise ValueError("远端备份索引字段无效")
    if value["format"] not in ("sxmqlive-signing-backup-index-v1", "ci-signing-backup-index-v2"):
        raise ValueError("远端备份索引格式无效")
    modern = value["format"] == "ci-signing-backup-index-v2"
    if set(value) != (INDEX_KEYS | policy_keys if modern else INDEX_KEYS):
        raise ValueError("备份索引范围字段无效")
    if modern and (not isinstance(value['projectId'], str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', value['projectId'])
                   or value['scope'] not in ('controller_dr', 'project_signing')):
        raise ValueError("备份索引范围无效")
    if project is not None:
        policy = project['signingBackup']
        if modern and (value['projectId'] != project['id'] or value['scope'] != policy['scope']):
            raise ValueError("远端备份分支属于其他项目或范围")
        if not modern and (project['id'] != 'sxmqlive' or policy['scope'] != 'controller_dr'
                           or value['recipientSha256'] != policy.get('legacyRecipientSha256')):
            raise ValueError("旧索引仅接受原 Controller 灾备接收人")
    if not isinstance(value["snapshot"], str) or not SNAPSHOT.fullmatch(value["snapshot"]):
        raise ValueError("远端快照路径无效")
    for key in ("contentSha256", "ciphertextSha256", "recipientSha256"):
        if not isinstance(value[key], str) or not HEX64.fullmatch(value[key]):
            raise ValueError("远端备份摘要无效")
    expected_recovery = "recipients/" + value["recipientSha256"] + ".enc.json"
    if value["recipientRecovery"] not in (None, expected_recovery):
        raise ValueError("远端恢复密文路径无效")
    if not isinstance(value["sourceCommit"], str) or not HEX40.fullmatch(value["sourceCommit"]):
        raise ValueError("远端来源提交无效")
    if type(value["buildNumber"]) is not int or value["buildNumber"] < 0:
        raise ValueError("远端构建编号无效")
    if not isinstance(value["createdAt"], str):
        raise ValueError("远端时间无效")
    stamp = datetime.strptime(value["createdAt"], "%Y-%m-%dT%H:%M:%S.%fZ")
    if stamp.strftime("%Y%m%dT%H%M%S%fZ") not in value["snapshot"]:
        raise ValueError("远端快照时间不一致")
    if not value["snapshot"].endswith("-" + value["contentSha256"][:12] + ".enc.json"):
        raise ValueError("远端快照摘要不一致")
    return value


def git_environment():
    # 不继承调用方的 Git 索引/目录注入；SSH 密钥仍由 Jenkins withCredentials 提供。
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    command = shlex.split(os.environ.get("GIT_SSH_COMMAND", ""))
    if not command or Path(command[0]).name != "ssh":
        raise ValueError("必须由调用方提供 SSH 凭据命令")
    options = " ".join(command).lower().replace(" ", "")
    if re.search(r"stricthostkeychecking=(no|off|false|accept-new)", options):
        raise ValueError("SSH 必须校验已知主机")
    if "userknownhostsfile=" in options or "globalknownhostsfile=" in options or "-F" in command:
        raise ValueError("SSH 必须继承现有 known_hosts")
    env["GIT_SSH_COMMAND"] = shlex.join(command[:1] + ["-o", "StrictHostKeyChecking=yes", "-o",
                                          "BatchMode=yes", "-o", "ConnectTimeout=15"] + command[1:])
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env


def git(repo, env, *args, check=True):
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                             "-c", "user.name=Signing Backup", "-c", "user.email=signing-backup@localhost",
                             *args], cwd=repo, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=120)
    if check and result.returncode:
        # Git stderr 可能包含 SSH 路径或连接信息，失败只暴露固定操作名。
        raise RuntimeError("Git " + args[0] + " 失败")
    return result


def remote_state(repo, env, project=None):
    branch = project['signingBackup']['branch'] if project else BRANCH
    ref, fetched = 'refs/heads/' + branch, 'refs/remotes/origin/' + branch
    output = git(repo, env, "ls-remote", "--heads", "origin", ref).stdout.decode("ascii").strip()
    if not output:
        return None, None
    fields = output.split()
    if len(fields) != 2 or fields[1] != ref or not HEX40.fullmatch(fields[0]):
        raise ValueError("远端分支响应无效")
    git(repo, env, "fetch", "--no-tags", "origin", ref + ":" + fetched)
    commit = git(repo, env, "rev-parse", fetched).stdout.decode("ascii").strip()
    rows = git(repo, env, "ls-tree", "-r", "-z", fetched).stdout.split(b"\0")
    paths = set()
    for row in filter(None, rows):
        metadata, raw_path = row.split(b"\t", 1)
        path = raw_path.decode("ascii")
        mode, kind, blob = metadata.decode("ascii").split()
        if mode != "100644" or kind != "blob" or (path != "latest.json" and
                not SNAPSHOT.fullmatch(path) and not RECOVERY.fullmatch(path)):
            raise ValueError("远端分支包含非备份文件")
        paths.add(path)
        size = int(git(repo, env, "cat-file", "-s", blob).stdout)
        maximum = 8192 if path == "latest.json" else (64 * 1024 if RECOVERY.fullmatch(path) else LIMIT)
        if size > maximum:
            raise ValueError("远端备份文件过大")
        if path != "latest.json":
            encrypted_snapshot(git(repo, env, "cat-file", "blob", blob).stdout, bool(RECOVERY.fullmatch(path)))
    # 每次只允许新增密文和更新公开索引，拒绝项目历史、合并和改写旧快照。
    for line in git(repo, env, "rev-list", "--parents", fetched).stdout.decode("ascii").splitlines():
        revisions = line.split()
        if len(revisions) > 2:
            raise ValueError("备份分支不得包含合并历史")
        index_ref = revisions[0] + ":latest.json"
        if int(git(repo, env, "cat-file", "-s", index_ref).stdout) > 8192:
            raise ValueError("历史备份索引过大")
        historical = validate_index(parse_json(git(repo, env, "show", index_ref).stdout), project)
        git(repo, env, "cat-file", "-e", revisions[0] + ":" + historical["snapshot"])
        if historical["recipientRecovery"]:
            git(repo, env, "cat-file", "-e", revisions[0] + ":" + historical["recipientRecovery"])
        changes = git(repo, env, "diff-tree", "--root", "--no-commit-id", "-r", "--name-status",
                      "--no-renames", revisions[0]).stdout.decode("ascii").splitlines()
        for change in changes:
            status, path = change.split("\t", 1)
            if not ((path == "latest.json" and status in {"A", "M"}) or
                    ((SNAPSHOT.fullmatch(path) or RECOVERY.fullmatch(path)) and status == "A")):
                raise ValueError("备份历史必须仅追加密文快照")
    if "latest.json" not in paths:
        raise ValueError("远端缺少备份索引")
    latest = validate_index(parse_json(git(repo, env, "show", fetched + ":latest.json").stdout), project)
    if latest["snapshot"] not in paths:
        raise ValueError("远端索引引用不存在的快照")
    if latest["recipientRecovery"] and latest["recipientRecovery"] not in paths:
        raise ValueError("远端索引引用不存在的恢复密文")
    raw = git(repo, env, "show", fetched + ":" + latest["snapshot"]).stdout
    sealed = encrypted_snapshot(raw)
    if (hashlib.sha256(raw).hexdigest() != latest["ciphertextSha256"] or
            sealed["recipientSha256"] != latest["recipientSha256"]):
        raise ValueError("远端密文校验失败")
    return commit, latest


def publish(snapshot, content_sha256, source_commit, build_number, recipient_recovery=None, project_id='sxmqlive'):
    project = load_project(project_id)
    policy = project['signingBackup']
    branch = policy['branch']
    ref, fetched = 'refs/heads/' + branch, 'refs/remotes/origin/' + branch
    remote = project['repositories'][policy['repository']]['ssh']
    if not HEX64.fullmatch(content_sha256) or not HEX40.fullmatch(source_commit) or build_number < 0:
        raise ValueError("摘要、提交或构建编号无效")
    if snapshot.is_symlink() or not snapshot.is_file() or snapshot.stat().st_size > LIMIT:
        raise ValueError("快照必须是受限大小的普通文件")
    raw = snapshot.read_bytes()
    sealed = encrypted_snapshot(raw)
    if sealed['recipientSha256'] != policy['recipientSha256']:
        raise ValueError('密文接收人与可信项目灾备/签名范围不一致')
    recovery_raw = None
    recovery_path = None
    if recipient_recovery is not None:
        if (recipient_recovery.is_symlink() or not recipient_recovery.is_file() or
                recipient_recovery.stat().st_size > 64 * 1024):
            raise ValueError("恢复密文必须是受限大小的普通文件")
        recovery_raw = recipient_recovery.read_bytes()
        encrypted_snapshot(recovery_raw, recovery=True)
        recovery_path = "recipients/" + sealed["recipientSha256"] + ".enc.json"
    env = git_environment()
    with tempfile.TemporaryDirectory(prefix="signing-backup-git-") as directory:
        repo = Path(directory)
        repo.chmod(0o700)
        git(repo, env, "init", "--quiet")
        git(repo, env, "remote", "add", "origin", remote)
        for attempt in range(3):
            remote_commit, latest = remote_state(repo, env, project)
            recovery_exists = False
            if remote_commit and recovery_path:
                recovery_exists = bool(git(repo, env, "ls-tree", fetched, "--", recovery_path).stdout)
                if recovery_exists and git(repo, env, "show", fetched + ":" + recovery_path).stdout != recovery_raw:
                    raise ValueError("已有接收密钥的恢复密文不得替换")
            if (latest and latest["contentSha256"] == content_sha256 and
                    latest["recipientSha256"] == sealed["recipientSha256"] and
                    (recovery_path is None or (recovery_exists and latest["recipientRecovery"] == recovery_path))):
                return report("unchanged", remote_commit, latest, project)
            if remote_commit:
                git(repo, env, "checkout", "-B", branch, fetched)
            else:
                git(repo, env, "checkout", "--orphan", branch)
            now = datetime.now(timezone.utc)
            path = "snapshots/" + now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + content_sha256[:12] + ".enc.json"
            latest = {"format": "ci-signing-backup-index-v2", "projectId": project['id'], "scope": policy['scope'], "snapshot": path,
                      "contentSha256": content_sha256, "ciphertextSha256": hashlib.sha256(raw).hexdigest(),
                      "sourceCommit": source_commit, "buildNumber": build_number,
                      "createdAt": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                      "recipientSha256": sealed["recipientSha256"], "recipientRecovery": recovery_path}
            destination = repo / path
            destination.parent.mkdir(exist_ok=True)
            with destination.open("xb") as output:
                output.write(raw)
            (repo / "latest.json").write_text(json.dumps(latest, sort_keys=True) + "\n", encoding="utf-8")
            git(repo, env, "add", "--", path, "latest.json")
            if recovery_path and not recovery_exists:
                recovery_destination = repo / recovery_path
                recovery_destination.parent.mkdir(exist_ok=True)
                with recovery_destination.open("xb") as output:
                    output.write(recovery_raw)
                git(repo, env, "add", "--", recovery_path)
            git(repo, env, "commit", "--quiet", "-m", "Append encrypted signing backup")
            committed = git(repo, env, "rev-parse", "HEAD").stdout.decode("ascii").strip()
            pushed = git(repo, env, "push", "origin", "HEAD:" + ref, check=False)
            verified_commit, verified = remote_state(repo, env, project)
            if (verified and verified["contentSha256"] == content_sha256 and
                    verified["recipientSha256"] == sealed["recipientSha256"] and
                    (recovery_path is None or (verified["recipientRecovery"] == recovery_path and
                     git(repo, env, "show", fetched + ":" + recovery_path).stdout == recovery_raw)) and
                    (verified_commit != committed or verified == latest)):
                return report("synced" if verified_commit == committed else "unchanged", verified_commit, verified, project)
            if not pushed.returncode:
                raise RuntimeError("推送后远端快照验证失败")
            if attempt == 2 or remote_commit == verified_commit:
                raise RuntimeError("Git push 失败；密文仍保留在调用方")
        raise RuntimeError("备份同步失败")


def report(status, commit="", latest=None, project=None):
    latest = latest or {}
    project = project or load_project()
    policy = project['signingBackup']
    return {"projectId": project['id'], "scope": policy['scope'], "recipientSha256": policy['recipientSha256'],
            "status": status, "branch": policy['branch'], "gitCommit": commit,
            "snapshot": latest.get("snapshot", ""), "contentSha256": latest.get("contentSha256", ""),
            "ciphertextSha256": latest.get("ciphertextSha256", ""),
            "recipientRecovery": latest.get("recipientRecovery")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="sxmqlive")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--content-sha256", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--build-number", type=int, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--recipient-recovery", type=Path)
    args = parser.parse_args(argv)
    result = report("pending", project=load_project(args.project))
    inputs = [args.snapshot] + ([args.recipient_recovery] if args.recipient_recovery else [])
    safe_report = all(args.report.resolve() != source.resolve() and
                      not (args.report.exists() and source.exists() and os.path.samefile(args.report, source))
                      for source in inputs)
    try:
        if not safe_report:
            raise ValueError("报告不能覆盖密文")
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result) + "\n", encoding="utf-8")
        result = publish(args.snapshot, args.content_sha256, args.source_commit, args.build_number,
                         args.recipient_recovery, args.project)
    finally:
        if safe_report:
            args.report.write_text(json.dumps(result) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("签名备份同步失败；检查连接、输入与 pending 报告。密文未删除，未输出凭据。")
