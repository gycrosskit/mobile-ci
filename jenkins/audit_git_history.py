#!/usr/bin/env python3
"""审核全部 Git refs 历史及本地对象；输出只含身份、位置和摘要，不含秘密。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

VERSION = '8.30.1'
MAX_OBJECTS = 100000
MAX_OBJECT_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_REPORT_BYTES = 64 * 1024 * 1024
OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')
# 仅批准逐字相同的公开代码跨度；不能按路径、目录或秘密值屏蔽规则。
APPROVED = {
    ('generic-api-key', 1, '204c0a5ae05e08da3dd9bdddccec44b2c7a0afa5e805c7f0645164d35e69ef32'),
    ('generic-api-key', 1, '4e65146fffb2c66a1decdf447b3fda802f9865d73ade17b497a19e5b0cd2b93f'),
    ('private-key', 2, '4a47b67dc69abc3135cceb8aafa45906c3b8965106c86d94ccd29cd2b8bfba6f'),
    ('private-key', 2, '9e79164d7fb8b53dfde019f6b34454da129b5d193d3b1dcb083eb5bb08d51d6d'),
    # 发行清单的四个 credentials 源码摘要已逐文件核实；只批准两种缩进的完整 JSON 行。
    ('generic-api-key', 1, '9879ac1db469e5734c34c986ccecbcc001d37531f51a3cc224498ea297ead12f'),
    ('generic-api-key', 1, '5a609069cf5baac9d53ad2cbff3e0136278e3266695a379cf76b4eada2d9ad7d'),
    ('generic-api-key', 1, 'df837789d8ab5d1ee0a3b481f37701559a474f513fabcc9c1134a1a141e20c79'),
    ('generic-api-key', 1, '69246ad7189f5902419439a97a1c92a7ce5c599152b69c874f423363dedc008a'),
    ('generic-api-key', 1, '7034ba951ec9fbe25294d8dbf20b05295437bce09dd88fe9bc56ed73ef672acf'),
    ('generic-api-key', 1, 'b8f86fcbf0d157687bfd8de20ba5716b36717c0fd7f11c0c4fa98e00766d2c1b'),
    ('generic-api-key', 1, '2663a3ce7e90441c415ca7fe4b1d0ed4ddff5c398ff7d625dcc61c8069c9aa5a'),
    ('generic-api-key', 1, '45209d09b7ecedb9730cb31fce3b9090994802e94614c10f64b4ee69d8765a8a'),
}


class AuditError(ValueError):
    pass


def environment():
    env = {key: value for key, value in os.environ.items() if not key.startswith(('GITLEAKS_', 'GIT_'))}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_REPLACE_OBJECTS='1', GIT_TERMINAL_PROMPT='0')
    return env


def regular_file(path, limit):
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise AuditError('拒绝符号链接文件')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > limit or metadata.st_mode & 0o022:
            raise AuditError('文件必须为有界、非其他用户可写的单链接普通文件')
        data = source.read(limit + 1)
        if len(data) > limit:
            raise AuditError('文件超过大小预算')
        return data


def git(repo, *args):
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(repo), *args], env=environment(),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
    if result.returncode:
        raise AuditError('Git 对象核验失败；未输出工具日志')
    return result.stdout


def inventory(repo):
    if git(repo, 'rev-parse', '--is-shallow-repository').strip() != b'false':
        raise AuditError('浅克隆不能证明完整历史')
    config = git(repo, 'config', '--local', '--list').decode('utf-8')
    if re.search(r'(?im)^(extensions\.partialclone|remote\..*\.promisor)=', config):
        raise AuditError('部分克隆不能证明完整对象覆盖')
    objects, total = {}, 0
    process = subprocess.Popen(['git', '--no-replace-objects', '-C', str(repo), 'cat-file', '--batch-all-objects', '--batch-check'],
                               env=environment(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        for line in process.stdout:
            fields = line.decode('ascii').split()
            if len(fields) != 3 or not OID.fullmatch(fields[0]) or fields[1] not in ('blob', 'commit', 'tag', 'tree') or not fields[2].isdigit():
                raise AuditError('未知 Git 对象元数据')
            oid, kind, size = fields[0], fields[1], int(fields[2])
            if oid in objects or size > MAX_OBJECT_BYTES:
                raise AuditError('对象重复或超过单对象预算；未跳过')
            objects[oid] = (kind, size); total += size
            if len(objects) > MAX_OBJECTS or total > MAX_TOTAL_BYTES:
                raise AuditError('对象数量或总大小超过预算；未跳过')
        if process.wait(timeout=120):
            raise AuditError('Git 对象枚举失败')
    finally:
        if process.poll() is None:
            process.kill(); process.wait()
        process.stdout.close()
    commits = git(repo, 'rev-list', '--all').decode('ascii').splitlines()
    if not commits or any(not OID.fullmatch(oid) or objects.get(oid, ('',))[0] != 'commit' for oid in commits):
        raise AuditError('refs 历史缺失或包含未知提交')
    return objects, total, commits


def materialize(repo, objects, directory):
    process = subprocess.Popen(['git', '--no-replace-objects', '-C', str(repo), 'cat-file', '--batch'], env=environment(),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        for oid, (kind, size) in objects.items():
            process.stdin.write((oid + '\n').encode()); process.stdin.flush()
            if process.stdout.readline() != ('{} {} {}\n'.format(oid, kind, size)).encode():
                raise AuditError('Git 对象回读元数据不一致')
            data = process.stdout.read(size)
            if len(data) != size or process.stdout.read(1) != b'\n':
                raise AuditError('Git 对象被截断')
            digest = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
            if digest(kind.encode() + b' ' + str(size).encode() + b'\0' + data).hexdigest() != oid:
                raise AuditError('Git 对象内容与身份不符')
            path = directory / (oid + '.' + kind)
            with path.open('xb') as output:
                os.chmod(path, 0o600); output.write(data)
        process.stdin.close()
        if process.wait(timeout=120):
            raise AuditError('Git 对象回读失败')
    finally:
        if process.poll() is None:
            process.kill(); process.wait()
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()


def scan(scanner, mode, target, temporary):
    report = temporary / (mode + '-report.json')
    args = [str(scanner), mode, str(target), '--config=' + str(temporary / 'default.toml'),
            '--gitleaks-ignore-path=' + str(temporary / 'empty.ignore'), '--ignore-gitleaks-allow',
            '--redact=100', '--report-format=json', '--report-path=' + str(report), '--no-banner', '--no-color', '--timeout=120',
            '--max-target-megabytes=0', '--max-decode-depth=5']
    if mode == 'git':
        args.append('--log-opts=--all --full-history --no-renames --no-ext-diff --no-textconv')
    # 工具日志可能携带 Match/Secret；只在私有临时目录捕获，永不写入最终报告。
    with (temporary / (mode + '.log')).open('xb') as log:
        os.chmod(log.name, 0o600)
        result = subprocess.run(args, env=environment(), stdout=log, stderr=log, cwd=temporary, timeout=150)
    if result.returncode not in (0, 1) or not report.exists():
        raise AuditError('扫描器异常或报告缺失；未输出工具日志')
    value = json.loads(regular_file(report, MAX_REPORT_BYTES))
    if not isinstance(value, list) or (result.returncode == 0 and value) or (result.returncode == 1 and not value):
        raise AuditError('扫描器状态与报告不一致')
    return value


def sanitize_finding(finding, mode, repo, objects, directory):
    if not isinstance(finding, dict) or not isinstance(finding.get('File'), str) or not isinstance(finding.get('RuleID'), str):
        raise AuditError('未知扫描结果格式')
    start, end = finding.get('StartLine'), finding.get('EndLine')
    if type(start) is not int or type(end) is not int or start < 1 or end < start:
        raise AuditError('扫描结果行号无效')
    file, commit = finding['File'], finding.get('Commit', '')
    if mode == 'git':
        if not isinstance(commit, str) or not OID.fullmatch(commit) or objects.get(commit, ('',))[0] != 'commit':
            raise AuditError('扫描结果提交不在完整对象清单')
        if not file or Path(file).is_absolute() or '..' in Path(file).parts or '\0' in file:
            raise AuditError('扫描结果文件路径无效')
        oid = git(repo, 'rev-parse', '--verify', commit + ':' + file).decode('ascii').strip()
        if objects.get(oid, ('',))[0] != 'blob':
            raise AuditError('扫描结果源码不是完整清单内 blob')
        path = directory / (oid + '.blob')
    else:
        path = Path(file)
        if not path.is_absolute():
            path = directory / path
        if path.parent != directory or not re.fullmatch(r'[0-9a-f]{40,64}\.(blob|commit|tag|tree)', path.name):
            raise AuditError('对象扫描结果路径越界')
        oid = path.name.split('.')[0]
        if oid not in objects or path.name != oid + '.' + objects[oid][0]:
            raise AuditError('对象扫描结果不在完整清单')
        file = 'objects/' + path.name
        commit = oid if objects[oid][0] == 'commit' else ''
    lines = regular_file(path, MAX_OBJECT_BYTES).splitlines()
    if end > len(lines):
        raise AuditError('扫描结果行号超过源对象')
    digest = hashlib.sha256(b'\n'.join(lines[start - 1:end])).hexdigest()
    approved = (finding['RuleID'], end - start + 1, digest) in APPROVED
    return {'path': file, 'objectId': oid, 'commit': commit, 'rule': finding['RuleID'],
            'line': start, 'endLine': end, 'hash': digest, 'approved': approved}


def audit(repo, scanner):
    repo, scanner = Path(repo).absolute(), Path(scanner).absolute()
    if not repo.is_dir() or any(item.is_symlink() for item in (repo, *repo.parents)):
        raise AuditError('仓库必须为未经过符号链接的目录')
    regular_file(scanner, 128 * 1024 * 1024)
    version = subprocess.run([str(scanner), 'version'], env=environment(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    if version.returncode or version.stdout.strip() != VERSION.encode():
        raise AuditError('必须由操作者显式安装固定 Gitleaks ' + VERSION)
    objects, total, commits = inventory(repo)
    reachable_objects = set(git(repo, 'rev-list', '--objects', '--all', '--no-object-names').decode('ascii').splitlines())
    if not reachable_objects <= objects.keys():
        raise AuditError('refs 对象与完整对象清单不一致')
    with tempfile.TemporaryDirectory(prefix='ci-history-audit-') as name:
        temporary = Path(name).resolve(); os.chmod(temporary, 0o700)
        directory = temporary / 'objects'; directory.mkdir(mode=0o700)
        for filename, text in (('default.toml', '[extend]\nuseDefault = true\n'), ('empty.ignore', '')):
            path = temporary / filename; path.write_text(text); path.chmod(0o600)
        materialize(repo, objects, directory)
        findings = [sanitize_finding(item, mode, repo, objects, directory)
                    for mode, target in (('git', repo), ('dir', directory)) for item in scan(scanner, mode, target, temporary)]
    approved = sum(item['approved'] for item in findings)
    return {'scannerVersion': VERSION, 'reachableCommits': len(commits), 'objects': len(objects), 'objectBytes': total,
            'unreachableObjects': len(objects) - len(reachable_objects),
            'objectTypes': {kind: sum(value[0] == kind for value in objects.values()) for kind in ('blob','commit','tag','tree')},
            'approvedCount': approved, 'unknownCount': len(findings) - approved, 'findings': findings}


def write_report(path, value):
    path = Path(path).absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise AuditError('报告路径不能使用符号链接')
    if path.exists():
        regular_file(path, MAX_REPORT_BYTES)
    if not path.parent.is_dir():
        raise AuditError('报告父目录必须已经存在')
    descriptor, name = tempfile.mkstemp(prefix='.history-audit-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(value, output, ensure_ascii=False, indent=2); output.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--scanner', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        value = audit(args.repo, args.scanner)
        write_report(args.report, value)
        print(json.dumps({key: value[key] for key in ('reachableCommits','objects','approvedCount','unknownCount')}))
        return 1 if value['unknownCount'] else 0
    except (AuditError, OSError, ValueError, subprocess.SubprocessError):
        print('历史秘密审核失败；未知/不完整输入或扫描器异常，未输出源码及工具日志。')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
