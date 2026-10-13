#!/usr/bin/env python3
"""管理员按受信 manifest 下载公开 Apple CA；不安装证书、不操作钥匙串。"""
import argparse
import ctypes
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

MANIFEST = Path(__file__).absolute().parent / 'resources/apple-intermediate-certificates/manifest.txt'
MAX_CERTIFICATE_BYTES = 131072


def no_links(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('路径及父目录不能使用符号链接')


def read_regular(path, limit):
    no_links(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_mode & 0o022 or metadata.st_size > limit:
            raise ValueError('文件必须是有界、不可由其他用户写入的单链接普通文件')
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError('文件超过大小限制')
    return data


def manifest_entries():
    entries = []
    for line in read_regular(MANIFEST, 65536).decode('utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        fields = line.split()
        if (len(fields) != 4 or not re.fullmatch(r'G[2-6]|DEV-ID-G[12]', fields[0])
                or not re.fullmatch(r'[0-9a-f]{64}', fields[1])
                or not re.fullmatch(r'[A-Za-z0-9_-]+\.pem', fields[2])
                or not re.fullmatch(r'https://www\.apple\.com/certificateauthority/[A-Za-z0-9_-]+\.cer', fields[3])):
            raise ValueError('Apple CA manifest 包含无效身份、摘要、文件名或非受信官方 URL')
        entries.append(fields)
    if len(entries) != 7 or len({entry[0] for entry in entries}) != 7 or len({entry[2] for entry in entries}) != 7:
        raise ValueError('必须声明 7 个唯一固定 Apple 中间证书')
    return entries


def download(url):
    # curl 默认验证 TLS；禁止跟随重定向，并限制连接、总时长和响应体。
    result = subprocess.run(['curl', '--disable', '--fail', '--silent', '--show-error', '--proto', '=https',
                             '--connect-timeout', '10', '--max-time', '20', '--max-filesize', str(MAX_CERTIFICATE_BYTES),
                             '--limit-rate', '128K', '--write-out', '\n%{http_code}', url], capture_output=True, check=True, timeout=25)
    if result.stdout[-4:] != b'\n200':
        raise ValueError('Apple CA 下载状态不匹配；不允许重定向')
    data = result.stdout[:-4]
    if not data or len(data) > MAX_CERTIFICATE_BYTES:
        raise ValueError('Apple CA 下载为空或超过大小限制')
    return data


def publish_exclusively(parent_descriptor, staging_name, output_name):
    # 普通 rename 会覆盖空目录；平台的 exclusive rename 保证并发占用时拒绝覆盖。
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        rename, flag = library.renameatx_np, 4
    elif sys.platform.startswith('linux') and hasattr(library, 'renameat2'):
        rename, flag = library.renameat2, 1
    else:
        raise ValueError('平台缺少无覆盖原子目录发布能力')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent_descriptor, os.fsencode(staging_name), parent_descriptor, os.fsencode(output_name), flag):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def fetch(output):
    output = output.absolute()
    no_links(output)
    if output.exists() or not output.parent.is_dir():
        raise ValueError('输出目录必须不存在，且父目录必须已经存在')
    parent_descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(parent_descriptor)
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise ValueError('输出父目录必须由当前管理员持有且不可由其他用户写入')
        entries = manifest_entries()
        with tempfile.TemporaryDirectory(prefix='.apple-ca-', dir=output.parent) as directory:
            staging = Path(directory)
            for alias, digest, filename, url in entries:
                der = download(url)
                if hashlib.sha256(der).hexdigest() != digest:
                    raise ValueError('Apple CA DER SHA-256 不匹配：' + alias)
                result = subprocess.run(['openssl', 'x509', '-inform', 'DER', '-outform', 'PEM'],
                                        input=der, capture_output=True, check=True, timeout=15)
                pem = staging / filename
                pem.write_bytes(result.stdout)
                pem.chmod(0o600)
                converted = subprocess.run(['openssl', 'x509', '-in', str(pem), '-outform', 'DER'],
                                           capture_output=True, check=True, timeout=15).stdout
                if hashlib.sha256(converted).hexdigest() != digest:
                    raise ValueError('Apple CA PEM 转换后 DER 摘要不匹配：' + alias)
            no_links(output)
            if output.parent.stat().st_ino != metadata.st_ino or output.parent.stat().st_dev != metadata.st_dev:
                raise ValueError('输出父目录在下载期间变化')
            publish_exclusively(parent_descriptor, staging.name, output.name)
    finally:
        os.close(parent_descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        fetch(args.output_dir)
        print('Downloaded and verified 7 pinned Apple intermediate certificates: ' + str(args.output_dir))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('Apple CA 下载失败：' + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
