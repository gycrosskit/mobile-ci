#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERTIFICATE_DIR="${APPLE_INTERMEDIATE_CERTIFICATE_DIR:-${SCRIPT_DIR}/resources/apple-intermediate-certificates}"
MANIFEST_PATH="${SCRIPT_DIR}/resources/apple-intermediate-certificates/manifest.txt"
[[ "${CERTIFICATE_DIR}" == /* ]] || { echo "Apple certificate directory must be absolute." >&2; exit 1; }

usage() {
  echo "Usage: $0 {verify|install <keychain-path>|check-system|install-system}"
}

require_tool() {
  local tool="$1"
  command -v "${tool}" >/dev/null || {
    echo "Missing certificate tool: ${tool}" >&2
    exit 1
  }
}

require_regular_file() {
  local path="$1" parent="$1" links
  while [[ "${parent}" != / && "${parent}" != . ]]; do
    [[ ! -L "${parent}" ]] || { echo "Apple certificate paths cannot contain symbolic links." >&2; exit 1; }
    parent="$(dirname "${parent}")"
  done
  [[ -f "${path}" ]] || { echo "Apple certificate file is missing or is not regular." >&2; exit 1; }
  if [[ "$(uname -s)" == Darwin ]]; then
    links="$(stat -f %l "${path}")"
  else
    links="$(stat -c %h "${path}")"
  fi
  [[ "${links}" == 1 ]] || { echo "Apple certificate file cannot use hard links." >&2; exit 1; }
}

certificate_der_sha256() {
  local certificate_path="$1"
  openssl x509 -in "${certificate_path}" -outform DER |
    shasum -a 256 |
    awk '{print $1}'
}

read_manifest() {
  require_regular_file "${MANIFEST_PATH}"
  [[ -f "${MANIFEST_PATH}" ]] || {
    echo "Apple intermediate certificate manifest is missing." >&2
    exit 1
  }
  grep -Ev '^[[:space:]]*(#|$)' "${MANIFEST_PATH}"
}

verify_bundle() {
  require_tool openssl
  require_tool shasum
  local alias expected_sha256 filename source_url extra certificate_path actual_sha256 count
  count=0
  while read -r alias expected_sha256 filename source_url extra; do
    [[ -n "${alias}" && -z "${extra:-}" ]] || {
      echo "Invalid Apple intermediate certificate manifest entry." >&2
      exit 1
    }
    [[ "${expected_sha256}" =~ ^[0-9a-f]{64}$ ]] || {
      echo "Invalid SHA-256 for Apple intermediate certificate ${alias}." >&2
      exit 1
    }
    [[ "${filename}" =~ ^[A-Za-z0-9._-]+\.pem$ ]] || {
      echo "Invalid filename for Apple intermediate certificate ${alias}." >&2
      exit 1
    }
    [[ "${source_url}" == https://www.apple.com/certificateauthority/* ]] || {
      echo "Apple intermediate certificate ${alias} has an untrusted source URL." >&2
      exit 1
    }
    certificate_path="${CERTIFICATE_DIR}/${filename}"
    require_regular_file "${certificate_path}"
    [[ -f "${certificate_path}" ]] || {
      echo "Apple intermediate certificate ${alias} is missing." >&2
      exit 1
    }
    actual_sha256="$(certificate_der_sha256 "${certificate_path}")"
    [[ "${actual_sha256}" == "${expected_sha256}" ]] || {
      echo "Apple intermediate certificate ${alias} failed SHA-256 verification." >&2
      exit 1
    }
    count=$((count + 1))
  done < <(read_manifest)
  [[ ${count} -eq 7 ]] || {
    echo "Fastlane 2.238.0 requires exactly 7 pinned Apple intermediate certificates." >&2
    exit 1
  }
  echo "Verified ${count} pinned Apple intermediate certificates."
}

install_bundle() {
  local keychain_path="$1" selected_alias="${2:-}"
  [[ -f "${keychain_path}" ]] || {
    echo "Target signing keychain does not exist." >&2
    exit 1
  }
  require_tool security
  verify_bundle

  local alias expected_sha256 filename source_url extra certificate_path available_hashes
  available_hashes="$(security find-certificate -a -Z "${keychain_path}")"
  while read -r alias expected_sha256 filename source_url extra; do
    [[ -z "${selected_alias}" || "${alias}" == "${selected_alias}" ]] || continue
    # 签名预检和 Profile 同步均可独立调用；仅跳过目标钥匙串中指纹一致的证书。
    if grep -Eiq "^SHA-256 hash: ${expected_sha256}$" <<< "${available_hashes}"; then
      continue
    fi
    certificate_path="${CERTIFICATE_DIR}/${filename}"
    security import "${certificate_path}" -k "${keychain_path}" >/dev/null
  done < <(read_manifest)

  available_hashes="$(security find-certificate -a -Z "${keychain_path}")"
  while read -r alias expected_sha256 filename source_url extra; do
    [[ -z "${selected_alias}" || "${alias}" == "${selected_alias}" ]] || continue
    grep -Eiq "^SHA-256 hash: ${expected_sha256}$" <<< "${available_hashes}" || {
      echo "Apple intermediate certificate ${alias} was not imported into the target keychain." >&2
      exit 1
    }
  done < <(read_manifest)
  echo "Pinned Apple intermediate certificates installed in the target keychain."
}

check_system_g3() {
  require_tool security
  verify_bundle
  local expected_sha256 available_hashes
  expected_sha256="$(read_manifest | awk '$1 == "G3" {print $2}')"
  available_hashes="$(security find-certificate -a -Z /Library/Keychains/System.keychain)"
  grep -Eiq "^SHA-256 hash: ${expected_sha256}$" <<< "${available_hashes}" || {
    echo "Pinned Apple WWDRG3 is missing from System.keychain; run the Mac Agent installer once." >&2
    return 1
  }
}

case "${1:-}" in
  verify)
    [[ $# -eq 1 ]] || {
      usage
      exit 2
    }
    verify_bundle
    ;;
  install)
    [[ $# -eq 2 ]] || {
      usage
      exit 2
    }
    install_bundle "$2"
    ;;
  check-system|install-system)
    [[ $# -eq 1 ]] || { usage; exit 2; }
    if [[ "$1" == install-system ]]; then
      [[ "$(id -u)" == 0 ]] || {
        echo "System certificate initialization requires an administrator." >&2
        exit 1
      }
      # CMS 的 BasicX509 链发现需要系统 G3；只导入公开 CA，不覆盖信任规则。
      install_bundle /Library/Keychains/System.keychain G3
    fi
    check_system_g3
    ;;
  *)
    usage
    exit 2
    ;;
esac
