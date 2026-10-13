#!/usr/bin/env python3
"""将 Jenkins 凭据与签名状态通过 SSH 收集，在本机加密；不落盘明文。"""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_config import load_project
from jenkins.remote.backup_resources import resources

LIMIT = 64 * 1024 * 1024


def entries(raw, prefix, private=False):
    result = {}
    total = 0
    if len(raw) > LIMIT:
        raise ValueError("备份过大")
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive.getmembers():
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or member.issym() or member.islnk():
                raise ValueError("备份包含不安全路径")
            if private and (member.mode & 0o777) != (0o700 if member.isdir() else 0o600):
                raise ValueError('签名材料必须保持私有目录0700/文件0600')
            if member.isdir():
                continue
            if not member.isfile() or member.size > LIMIT:
                raise ValueError("备份包含不支持的材料")
            key = prefix + "/" + str(name)
            if key in result or len(result) >= 2048:
                raise ValueError("备份包含重复材料")
            total += member.size
            if total > LIMIT:
                raise ValueError("备份材料总量过大")
            result[key] = archive.extractfile(member).read()
    return result


def snapshot(materials):
    if not materials or sum(map(len, materials.values())) > LIMIT:
        raise ValueError("备份为空或过大")
    manifest = {name: {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                for name, value in materials.items()}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, value in {**materials, "manifest.json": json.dumps(manifest).encode()}.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(value), 0o600
            archive.addfile(info, io.BytesIO(value))
    return output.getvalue()


def collect_materials(ssh, project, config):
    policy = project['signingBackup']
    target = resources(config, project, controller_dr=policy['scope'] == 'controller_dr')
    commands = []
    if policy['scope'] == 'controller_dr':
        commands.append((shlex.join(['docker', 'exec', target['controller'], 'bash', '-c',
            'cd /var/jenkins_home && find jobs -name config.xml -type f -print0 | tar --null -T - -cf - credentials.xml secrets config.xml']), 'controller'))
    elif policy['scope'] != 'project_signing' or not policy['prefixes']:
        raise ValueError('项目备份必须有明确签名材料范围')
    paths = ['.'] if policy['scope'] == 'controller_dr' else policy['prefixes']
    commands.append((shlex.join(['docker', 'exec', target['maintenance'], 'tar', '-C', policy['root'], '-cf', '-', '--', *paths]), 'signing-state'))
    materials = {}
    for command, prefix in commands:
        result = subprocess.run(ssh + [command], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
        if result.returncode:
            raise ValueError('无法读取远端私有材料')
        value = entries(result.stdout, prefix, private=prefix == 'signing-state')
        if prefix == 'signing-state' and policy['scope'] == 'project_signing':
            if any(not any(name == 'signing-state/' + path or name.startswith('signing-state/' + path + '/')
                           for path in policy['prefixes']) for name in value):
                raise ValueError('项目包拒绝范围外材料及 Controller 全局秘密')
        materials.update(value)
    if policy['scope'] == 'controller_dr':
        required = ['controller/credentials.xml', 'controller/config.xml', 'controller/secrets/master.key',
                    'controller/secrets/hudson.util.Secret']
        if any(name not in materials for name in required):
            raise ValueError('Controller 灾备恢复所需材料不完整')
    elif any(name.startswith('controller/') for name in materials):
        raise ValueError('项目签名包不得包含 Controller 全局秘密')
    if not materials:
        raise ValueError('签名材料为空')
    materials['backup-scope.json'] = json.dumps({'projectId': project['id'], 'scope': policy['scope'],
        'recipientSha256': policy['recipientSha256']}, sort_keys=True).encode()
    return materials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-control")
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument('--project', default='sxmqlive')
    parser.add_argument('--compose-file', required=True, help='远端管理员私有完整 Compose 清单绝对路径')
    args = parser.parse_args()
    project = load_project(args.project)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", args.ssh_host):
        raise ValueError("SSH 主机无效")
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if args.ssh_control:
        ssh += ["-S", args.ssh_control]
    ssh += [args.ssh_host]
    # 管理员显式选择清单；远端先验证权限，绝不退回默认项目的容器。
    check = '''import os,stat,sys,subprocess
from pathlib import Path
p=Path(sys.argv[1])
if not p.is_absolute() or not p.is_file() or p.resolve()!=p: raise SystemExit(1)
f,d=p.stat(),p.parent.stat()
if f.st_uid!=os.getuid() or stat.S_IMODE(f.st_mode)!=0o600 or f.st_nlink!=1 or d.st_uid!=os.getuid() or stat.S_IMODE(d.st_mode)!=0o700: raise SystemExit(1)
raise SystemExit(subprocess.call(['docker','compose','-f',str(p),'config','--format','json']))'''
    rendered = subprocess.run(ssh + [shlex.join(['python3', '-c', check, args.compose_file])],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if rendered.returncode:
        raise ValueError('远端完整 Compose 清单不可用或不私有')
    materials = collect_materials(ssh, project, json.loads(rendered.stdout))
    helper = Path(__file__).with_name("signing_backup.cjs")
    result = subprocess.run(["node", str(helper), "encrypt", str(args.key), str(args.output)],
                            input=snapshot(materials), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if result.returncode:
        raise ValueError("加密或恢复校验失败")
    print(json.dumps({"encrypted": True, "entries": len(materials), "verified": json.loads(result.stdout)["verified"]}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("签名备份失败：检查连接、权限和恢复密钥；未输出秘密。")
