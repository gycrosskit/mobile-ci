#!/usr/bin/env bash
# 三个隔离 Agent 通过共享 inode 互斥；不把凭据、缓存或工作区合并。
set +x
set -euo pipefail
umask 077

[[ $# -gt 0 ]] || { echo 'Missing Linux build command.' >&2; exit 64; }
[[ "${CI_LINUX_MEMORY_LOCK:-}" == /home/jenkins/linux-build-memory/build.lock ]] || {
  echo 'Linux build memory lock is not configured at the fixed Agent path.' >&2
  exit 72
}
command -v flock >/dev/null || { echo 'Linux build requires flock.' >&2; exit 72; }

# 先拒绝 FIFO，避免只读打开时阻塞；不同容器必须挂同一个预建 inode。
python3 - <<'PY'
import os
import stat
from pathlib import Path
path = Path(os.environ['CI_LINUX_MEMORY_LOCK'])
try:
    parent, current = path.parent.stat(), path.stat()
    valid = (not any(part.is_symlink() for part in (path, *path.parents))
             and path.resolve() == path and path.parent.is_dir() and stat.S_ISREG(current.st_mode)
             and stat.S_IMODE(parent.st_mode) == 0o700 and stat.S_IMODE(current.st_mode) == 0o600
             and parent.st_uid == current.st_uid == os.getuid())
except OSError:
    valid = False
if not valid:
    raise SystemExit('Linux build memory lock must be a private existing file without symlinks.')
PY
# 不创建、截断或替换锁；打开后再核验 inode，拒绝检查期间被换掉的路径。
{ exec 9<"${CI_LINUX_MEMORY_LOCK}"; } 2>/dev/null || {
  echo 'Linux build memory lock is missing.' >&2
  exit 72
}
python3 - <<'PY'
import os
import stat
from pathlib import Path
path = Path(os.environ['CI_LINUX_MEMORY_LOCK'])
try:
    current, opened = path.stat(), os.fstat(9)
    valid = (not any(part.is_symlink() for part in (path, *path.parents))
             and stat.S_ISREG(opened.st_mode)
             and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino))
except OSError:
    valid = False
if not valid:
    raise SystemExit('Linux build memory lock changed during validation.')
PY
flock --exclusive 9
# Bash 保留 FD 到命令退出；不能 exec 替换为可能主动关闭无关 FD 的 JVM。
set +e
"$@"
result=$?
exit "${result}"
