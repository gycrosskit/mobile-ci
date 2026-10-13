#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "${SCRIPT_DIR}/.fastlane-version")"
[[ "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo 'Invalid .fastlane-version' >&2; exit 2; }
RUBY_BIN="${FASTLANE_RUBY:-}"
if [[ -z "${RUBY_BIN}" ]]; then
  RUBY_BIN="$(brew --prefix ruby)/bin/ruby"
fi
[[ -x "${RUBY_BIN}" ]] || { echo 'Homebrew Ruby is required for fastlane.' >&2; exit 1; }
RUBY_ABI="$("${RUBY_BIN}" -rrbconfig -e 'puts RbConfig::CONFIG["ruby_version"]')"

# 版本选择与 gem 目录不依赖 PATH 上恰好安装的 fastlane，也不随临时签名 HOME 改变。
export GEM_HOME="${FASTLANE_GEM_HOME:-${HOME}/.local/share/fastlane/${RUBY_ABI}}"
export GEM_PATH="${GEM_HOME}"
export FASTLANE_SKIP_UPDATE_CHECK=1 FASTLANE_HIDE_CHANGELOG=1 FASTLANE_DISABLE_COLORS=1 FASTLANE_OPT_OUT_USAGE=1
if [[ "${1:-}" == '--install' ]]; then
  exec "${RUBY_BIN}" "${RUBY_BIN%/*}/gem" install fastlane --version "${VERSION}" --no-document
fi
cd "${SCRIPT_DIR}"
exec "${RUBY_BIN}" -e 'version = ARGV.shift; gem "fastlane", "=#{version}"; load Gem.bin_path("fastlane", "fastlane", version)' "${VERSION}" "$@"
