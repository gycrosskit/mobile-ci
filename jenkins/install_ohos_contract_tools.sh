#!/usr/bin/env bash
set -euo pipefail

[[ "$(uname -sm)" == "Linux x86_64" ]] || {
  echo '鸿蒙契约工具安装入口仅支持 Linux x86_64。' >&2
  exit 1
}
TOOLS_DIR="${1:?需提供工具安装目录}"
OHPM_ARCHIVE="${OHPM_ARCHIVE:-${HOME}/agent/tools/ohpm-26.0.0.630.tar.gz}"
[[ -f "${OHPM_ARCHIVE}" ]] || {
  echo '需由管理员预装官方 OHPM 26.0.0.630 工具归档；参见 remote runbook。' >&2
  exit 1
}
mkdir -p "${TOOLS_DIR}/node"
DOWNLOAD_DIR="$(mktemp -d "${TOOLS_DIR}/download.XXXXXX")"
trap 'rm -rf "${DOWNLOAD_DIR}"' EXIT

# 独立官方包用于 Node 契约，不安装 IDE、SDK 或签名工具。
curl --fail --silent --show-error --location --retry 2 --connect-timeout 30 --max-time 600 \
  https://nodejs.org/dist/v24.14.1/node-v24.14.1-linux-x64.tar.gz \
  --output "${DOWNLOAD_DIR}/node.tar.gz"
(
  cd "${DOWNLOAD_DIR}"
  printf '%s  %s\n' \
    ace9fa104992ed0829642629c46ca7bd7fd6e76278cb96c958c4b387d29658ea node.tar.gz \
    | sha256sum --check
)
tar -xzf "${DOWNLOAD_DIR}/node.tar.gz" --strip-components=1 -C "${TOOLS_DIR}/node"
printf '%s  %s\n' 04728d45137f3eaa71b72851fc6a13658b2a8dee5eeca85b7467fc80e99d1864 "${OHPM_ARCHIVE}" | sha256sum --check
tar -xzf "${OHPM_ARCHIVE}" -C "${TOOLS_DIR}"
export PATH="${TOOLS_DIR}/node/bin:${PATH}"
export NODE_HOME="${TOOLS_DIR}/node/bin"
node --version
[[ "$("${TOOLS_DIR}/ohpm/bin/ohpm" --version)" == "26.0.0.630" ]]
node -e 'if (require(process.argv[1]).version !== "4.9.5") throw new Error("TypeScript 版本不匹配")' \
  "${TOOLS_DIR}/ohpm/node_modules/typescript"
