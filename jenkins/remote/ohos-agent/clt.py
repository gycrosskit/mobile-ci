#!/usr/bin/env python3
"""官方 CLT 的离线安装与无签名预检；不下载、不接受许可、不修改既有 SDK。"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile

# 独立镜像保留常量；toolchains.py check 在晋级时核对根工具链锁，不能单独升级。
VERSION = '26.0.0.851'
NODE_VERSION = '24.14.1'
OHPM_VERSION = '26.0.0.630'
HVIGOR_VERSION = '6.26.8'
SDK_VERSION = '26.0.0.105'
SHA256 = 'ab604bd92721d5cbcafd154e6461d46b9f1b105e7b89eab04a9d046681198082'
LICENSE_SHA256 = '12730a76ed4c8b6acb20014d6303a32e3b8acbc8de742e1d6da683b3f81f9024'
MAX_FILES, MAX_BYTES, MAX_FILE_BYTES = 250_000, 20 * 1024**3, 2 * 1024**3
MAX_LINK_DEPTH, MAX_LINK_STEPS = 32, 250_000
SIGNING_JARS = ('hap-sign-tool.jar', 'binary-sign-tool.jar', 'app_packing_tool.jar',
                'app_unpacking_tool.jar', 'app_check_tool.jar')
HMS_SIGNING_JARS = ('Provisionsigntool.jar', 'sdk-sign-tool-full.jar')


def digest(stream):
    result = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        result.update(chunk)
    return result.hexdigest()


def check_tree(root, runtime=False):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('CLT must be a real directory')
    with (root / 'LICENSE.txt').open('rb') as stream:
        if digest(stream) != LICENSE_SHA256:
            raise ValueError('CLT license differs from the reviewed official package')
    if not re.search(r'^# Version: ' + re.escape(VERSION) + r'$',
                     (root / 'version.txt').read_text(), re.M):
        raise ValueError('CLT version mismatch')
    for relative, expected in (('hvigor/hvigor/package.json', HVIGOR_VERSION),
                               ('hvigor/hvigor-ohos-plugin/package.json', HVIGOR_VERSION),
                               ('ohpm/package.json', OHPM_VERSION)):
        if json.loads((root / relative).read_text()).get('version') != expected:
            raise ValueError('CLT tool version mismatch: ' + relative)
    sdk = root / 'sdk/default/openharmony'
    for namespace, manifest in (('openharmony', 'oh-uni-package.json'), ('hms', 'uni-package.json')):
        for component in ('native', 'ets', 'toolchains', 'js', 'previewer'):
            metadata = json.loads((root / 'sdk/default' / namespace / component / manifest).read_text())
            if metadata.get('apiVersion') != '26' or metadata.get('version') != SDK_VERSION:
                raise ValueError('SDK version mismatch: ' + namespace + '/' + component)
    for namespace, names in (('openharmony', SIGNING_JARS), ('hms', HMS_SIGNING_JARS)):
        for name in names:
            path = root / 'sdk/default' / namespace / 'toolchains/lib' / name
            if path.is_symlink() or not zipfile.is_zipfile(path):
                raise ValueError('SDK signing/packing tool missing or invalid: ' + name)
    for relative in ('tool/node/bin/node', 'bin/hvigorw', 'ohpm/bin/ohpm',
                     'sdk/default/openharmony/native/llvm/bin/clang'):
        if not os.access(root / relative, os.X_OK):
            raise ValueError('CLT executable missing: ' + relative)
    if runtime:
        if os.getuid() == 0:
            raise ValueError('Agent runtime must not run as root')
        with tempfile.TemporaryDirectory(prefix='ohos-preflight-') as home:
            environment = {**os.environ, 'HOME': home, 'NODE_HOME': str(root / 'tool/node'),
                           'DEVECO_NODE_HOME': str(root / 'tool/node'),
                           'PATH': str(root / 'tool/node/bin') + ':' + os.environ.get('PATH', '')}
            for command, expected in ((['java', '-version'], r'version "21[.\"]'),
                                      ([str(root / 'tool/node/bin/node'), '--version'], r'^v' + re.escape(NODE_VERSION) + '$'),
                                      (['bash', str(root / 'ohpm/bin/ohpm'), '--version'], r'^' + re.escape(OHPM_VERSION) + '$'),
                                      (['bash', str(root / 'bin/hvigorw'), '--version'], r'^' + re.escape(HVIGOR_VERSION) + '$'),
                                      ([str(sdk / 'native/llvm/bin/clang'), '--version'], r'clang version 15\.0\.4'),
                                      (['java', '-jar', str(sdk / 'toolchains/lib/hap-sign-tool.jar'), '-h'], r'sign-app')):
                result = subprocess.run(command, env=environment, cwd=home, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
                if result.returncode or not re.search(expected, result.stdout.strip(), re.M):
                    raise ValueError('CLT runtime preflight failed: ' + Path(command[0]).name)


def unpack(archive, stage):
    """先校验完整目录与链接图，最后折叠到普通成员，避免链接成为写入父目录。"""
    entries, links, total = {}, {}, 0
    infos = archive.infolist()
    if len(infos) > MAX_FILES:
        raise ValueError('Archive member limit exceeded')
    for info in infos:
        name = info.filename.rstrip('/')
        path = PurePosixPath(name)
        if (not name or info.orig_filename != info.filename or '\\' in name or ':' in name
                or path.is_absolute() or any(part in ('', '.', '..') for part in name.split('/'))
                or name in entries or info.flag_bits & 1
                or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
            raise ValueError('Unsafe or duplicate archive path')
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK):
            raise ValueError('Archive contains a special file')
        if kind and ((kind == stat.S_IFDIR) != info.is_dir()):
            raise ValueError('Archive file type disagrees with path')
        if mode & (stat.S_ISUID | stat.S_ISGID):
            raise ValueError('Archive contains privileged permissions')
        total += info.file_size
        if (info.file_size > MAX_FILE_BYTES or total > MAX_BYTES
                or info.file_size > max(1, info.compress_size) * 1000):
            raise ValueError('Archive expansion limit exceeded')
        entries[name] = info
        if kind == stat.S_IFLNK:
            if info.file_size > 4096:
                raise ValueError('Archive link too long')
            target = archive.read(info).decode('utf-8')
            if not target or '\\' in target or ':' in target or '\x00' in target or target.startswith('/'):
                raise ValueError('Unsafe archive link')
            resolved = os.path.normpath(str(path.parent / target))
            if resolved.startswith('../') or resolved == '..':
                raise ValueError('Archive link escapes root')
            links[name] = (target, resolved)
    roots = {PurePosixPath(name).parts[0] for name in entries}
    if len(roots) != 1:
        raise ValueError('Archive must have exactly one CLT root directory')
    root_name = next(iter(roots))
    for name, info in entries.items():
        for parent in PurePosixPath(name).parents:
            if str(parent) in entries and not entries[str(parent)].is_dir():
                raise ValueError('Archive parent is not a directory')
    resolved_links, steps = {}, 0
    for name in links:
        target, visited = name, set()
        while target in links:
            if target in visited:
                raise ValueError('Archive link cycle')
            visited.add(target)
            steps += 1
            if len(visited) > MAX_LINK_DEPTH or steps > MAX_LINK_STEPS:
                raise ValueError('Archive link resolution limit exceeded')
            target = links[target][1]
            if target != root_name and not target.startswith(root_name + '/'):
                raise ValueError('Archive link escapes CLT directory')
            if target not in entries:
                raise ValueError('Archive link target is missing')
        if entries[target].is_dir():
            raise ValueError('Archive link must target a regular member')
        # 官方 clang++ → clang → clang-15 保留入口名，但落盘链接只指向已验证的普通文件。
        resolved_links[name] = os.path.relpath(target, str(PurePosixPath(name).parent))
    for name, info in entries.items():
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if info.is_dir():
            target.mkdir(exist_ok=True)
        elif name not in links:
            with archive.open(info) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            target.chmod(0o755 if info.external_attr >> 16 & 0o111 else 0o644)
    for name, target in resolved_links.items():
        (stage / name).symlink_to(target)
    return stage / root_name


def install(archive_path, destination, license_reviewed=False):
    if not license_reviewed:
        raise ValueError('Administrator must review the official license before installation')
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Refusing to replace an existing CLT destination')
    if destination.parent.resolve() != destination.parent or not destination.parent.is_dir():
        raise ValueError('Destination parent must exist without symlinks')
    # 先复制到私有暂存区再验摘要，避免源文件在校验后被原地修改。
    descriptor = os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024**3:
            raise ValueError('CLT archive must be a regular file below 4 GiB')
        with tempfile.TemporaryDirectory(prefix='.clt-install-', dir=destination.parent) as temporary:
            copied = Path(temporary) / 'official.zip'
            with copied.open('xb') as output:
                size = 0
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    size += len(chunk)
                    if size > 4 * 1024**3:
                        raise ValueError('CLT archive exceeds 4 GiB')
                    output.write(chunk)
            with copied.open('rb') as verified:
                if digest(verified) != SHA256:
                    raise ValueError('Official CLT archive SHA-256 mismatch')
            with zipfile.ZipFile(copied) as archive:
                extracted = unpack(archive, Path(temporary))
            check_tree(extracted)
            # mkdir 保证并发时不替换管理员原有目录，失败只回收本次创建的目的地。
            destination.mkdir(mode=0o755)
            try:
                for item in extracted.iterdir():
                    item.rename(destination / item.name)
                (destination / 'ci-install-receipt.json').write_text(json.dumps({
                    'version': VERSION, 'archiveSha256': SHA256,
                    'licenseSha256': LICENSE_SHA256, 'administratorLicenseReviewed': True}) + '\n')
            except BaseException:
                shutil.rmtree(destination)
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    installer = commands.add_parser('install')
    installer.add_argument('archive', type=Path)
    installer.add_argument('destination', type=Path)
    installer.add_argument('--license-reviewed', action='store_true')
    checker = commands.add_parser('check')
    checker.add_argument('directory', type=Path)
    checker.add_argument('--runtime', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'install':
            install(args.archive, args.destination, args.license_reviewed)
        else:
            check_tree(args.directory, args.runtime)
    except (ValueError, OSError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        parser.exit(1, 'CLT validation failed: ' + str(error) + '\n')
    print('CLT ' + VERSION + ' validated' + (' (runtime)' if getattr(args, 'runtime', False) else ''))


if __name__ == '__main__':
    main()
