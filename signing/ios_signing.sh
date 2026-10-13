#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${APP_SOURCE_ROOT:-$(pwd)}"
STATE_DIR_INPUT="${IOS_SIGNING_STATE_DIR:-${REPO_ROOT}/build/ci-ios/signing}"
if [[ "${STATE_DIR_INPUT}" == /* ]]; then
  STATE_DIR="${STATE_DIR_INPUT}"
else
  STATE_DIR="$(pwd)/${STATE_DIR_INPUT}"
fi
KEYCHAIN_PATH="${STATE_DIR}/jenkins-ios.keychain-db"
KEYCHAIN_LIST_FILE="${STATE_DIR}/original-keychains.txt"
SIGNING_ENV_FILE="${STATE_DIR}/signing.env"
KEYCHAIN_PASSWORD_FILE="${STATE_DIR}/keychain-password.txt"
INSTALLED_PROFILE_PATH_FILE="${STATE_DIR}/installed-profile-path.txt"
PROFILE_BACKUP_FILE="${STATE_DIR}/profile-backup.mobileprovision"
PREFLIGHT_BINARY="${STATE_DIR}/signing-preflight"

usage() {
  echo "Usage: $0 {prepare|install-certificate|install-profile|install|verify-ipa|cleanup}"
}

read_keychain_list() {
  local source_file="$1"
  KEYCHAINS=()
  while IFS= read -r line; do
    line="${line#*\"}"
    line="${line%\"*}"
    if [[ -n "${line}" ]]; then
      KEYCHAINS+=("${line}")
    fi
  done < "${source_file}"
}

cleanup_signing() {
  local installed_profile="" restore_failed=0
  rm -f "${STATE_DIR}/profile.plist"
  if [[ -f "${INSTALLED_PROFILE_PATH_FILE}" ]]; then
    installed_profile="$(<"${INSTALLED_PROFILE_PATH_FILE}")"
    case "${installed_profile}" in
      "${HOME}/Library/MobileDevice/Provisioning Profiles/"*.mobileprovision)
        if [[ -f "${PROFILE_BACKUP_FILE}" ]]; then
          install -m 600 "${PROFILE_BACKUP_FILE}" "${installed_profile}"
        else
          rm -f "${installed_profile}"
        fi
        ;;
      *)
        echo "Refusing to remove unexpected provisioning profile path: ${installed_profile}" >&2
        ;;
    esac
  fi

  if [[ -f "${KEYCHAIN_LIST_FILE}" ]]; then
    read_keychain_list "${KEYCHAIN_LIST_FILE}"
    if [[ ${#KEYCHAINS[@]} -gt 0 ]]; then
      security list-keychains -d user -s "${KEYCHAINS[@]}" || {
        echo "Cannot restore keychain search list; retaining original-keychains.txt for cleanup retry." >&2
        restore_failed=1
      }
    fi
  fi
  if [[ -f "${KEYCHAIN_PATH}" ]]; then
    security delete-keychain "${KEYCHAIN_PATH}" >/dev/null 2>&1 || {
      echo "Cannot delete temporary signing keychain; retaining signing state for cleanup retry." >&2
      return 1
    }
  fi
  rm -f \
    "${SIGNING_ENV_FILE}" \
    "${INSTALLED_PROFILE_PATH_FILE}" \
    "${KEYCHAIN_PASSWORD_FILE}" \
    "${PROFILE_BACKUP_FILE}" \
    "${PREFLIGHT_BINARY}"
  if [[ ${restore_failed} -eq 0 ]]; then
    rm -f "${KEYCHAIN_LIST_FILE}"
  fi
  rmdir "${STATE_DIR}" >/dev/null 2>&1 || true
  if [[ ${restore_failed} -ne 0 ]]; then
    return 1
  fi
  echo "iOS temporary signing state cleaned."
}

prepare_keychain() {
  command -v security >/dev/null
  command -v openssl >/dev/null
  if [[ -f "${KEYCHAIN_PATH}" && -f "${KEYCHAIN_PASSWORD_FILE}" && -f "${SIGNING_ENV_FILE}" ]]; then
    security unlock-keychain -p "$(<"${KEYCHAIN_PASSWORD_FILE}")" "${KEYCHAIN_PATH}"
    return
  fi
  [[ ! -e "${KEYCHAIN_PATH}" && ! -e "${KEYCHAIN_LIST_FILE}" ]] || {
    echo "Refusing to reuse incomplete iOS signing state." >&2
    exit 1
  }
  mkdir -p "${STATE_DIR}"
  security list-keychains -d user > "${KEYCHAIN_LIST_FILE}"
  local keychain_password
  keychain_password="$(openssl rand -hex 24)"
  printf '%s\n' "${keychain_password}" > "${KEYCHAIN_PASSWORD_FILE}"
  chmod 600 "${KEYCHAIN_PASSWORD_FILE}"
  security create-keychain -p "${keychain_password}" "${KEYCHAIN_PATH}"
  # 临时钥匙串仍有六小时超时；Agent 休眠不应在 archive 中途重新要求手工解锁。
  security set-keychain-settings -ut 21600 "${KEYCHAIN_PATH}"
  security unlock-keychain -p "${keychain_password}" "${KEYCHAIN_PATH}"

  read_keychain_list "${KEYCHAIN_LIST_FILE}"
  # macOS Bash 3.2 在 set -u 下不能展开空数组；新服务账号可能没有既有钥匙串。
  if [[ ${#KEYCHAINS[@]} -gt 0 ]]; then
    security list-keychains -d user -s "${KEYCHAIN_PATH}" "${KEYCHAINS[@]}"
  else
    security list-keychains -d user -s "${KEYCHAIN_PATH}"
  fi
  {
    printf 'IOS_KEYCHAIN_PATH=%s\n' "${KEYCHAIN_PATH}"
    printf 'IOS_KEYCHAIN_PASSWORD_FILE=%s\n' "${KEYCHAIN_PASSWORD_FILE}"
  } > "${SIGNING_ENV_FILE}"
  echo "iOS temporary signing keychain prepared."
}

install_certificate() {
  [[ -n "${IOS_TEAM_ID:-}" ]] || {
    echo "Missing IOS_TEAM_ID for iOS signing preflight." >&2
    return 1
  }
  : "${IOS_DISTRIBUTION_P12:?Missing IOS_DISTRIBUTION_P12}"
  [[ -f "${IOS_DISTRIBUTION_P12}" ]] || {
    echo "iOS distribution certificate file does not exist." >&2
    exit 1
  }
  local distribution_password
  if [[ -n "${IOS_DISTRIBUTION_P12_PASSWORD_FILE:-}" ]]; then
    [[ -f "${IOS_DISTRIBUTION_P12_PASSWORD_FILE}" ]] || {
      echo "iOS distribution certificate password file does not exist." >&2
      exit 1
    }
    distribution_password="$(<"${IOS_DISTRIBUTION_P12_PASSWORD_FILE}")"
  else
    : "${IOS_DISTRIBUTION_P12_PASSWORD:?Missing IOS_DISTRIBUTION_P12_PASSWORD}"
    distribution_password="${IOS_DISTRIBUTION_P12_PASSWORD}"
  fi
  [[ -n "${distribution_password}" ]] || {
    echo "iOS distribution certificate password is empty." >&2
    exit 1
  }
  prepare_keychain
  local keychain_password preflight_hash identity_hash
  keychain_password="$(<"${KEYCHAIN_PASSWORD_FILE}")"
  xcrun --sdk macosx clang -Werror -Wno-deprecated-declarations \
    "${SCRIPT_DIR}/ios_signing_preflight.c" -framework Security -framework CoreFoundation \
    -o "${PREFLIGHT_BINARY}"
  /usr/bin/codesign --force --sign - "${PREFLIGHT_BINARY}"
  preflight_hash="$(/usr/bin/codesign -d --verbose=4 "${PREFLIGHT_BINARY}" 2>&1 | sed -n 's/^CDHash=//p')"
  [[ "${preflight_hash}" =~ ^[0-9a-fA-F]{40}$ ]] || {
    echo "Cannot determine signing preflight binary CDHash." >&2
    return 1
  }
  # Jenkins 绑定文件的临时名不保证 .p12 后缀，不能依赖 Security 自动识别。
  security import "${IOS_DISTRIBUTION_P12}" \
    -f pkcs12 \
    -k "${KEYCHAIN_PATH}" \
    -P "${distribution_password}" \
    -T /usr/bin/codesign \
    -T /usr/bin/security \
    -T "${PREFLIGHT_BINARY}" >/dev/null
  security set-key-partition-list \
    -S "apple-tool:,apple:,cdhash:${preflight_hash}" \
    -s \
    -k "${keychain_password}" \
    "${KEYCHAIN_PATH}" >/dev/null
  bash "${SCRIPT_DIR}/ios_apple_intermediate_certificates.sh" install "${KEYCHAIN_PATH}"
  identity_hash="$(security find-identity -v -p codesigning "${KEYCHAIN_PATH}" | \
    awk -v team="${IOS_TEAM_ID}" '/"Apple Distribution:/ && index($0, "(" team ")\"") {print $2}')"
  [[ "${identity_hash}" =~ ^[0-9A-F]{40}$ ]] || {
    echo "Expected exactly one valid Apple Distribution identity for the selected Team in the temporary keychain." >&2
    return 1
  }
  # stdin 关闭不能阻止原生授权弹窗；由同一原生进程显式禁用 Security 交互并签名。
  python3 - "${PREFLIGHT_BINARY}" "${KEYCHAIN_PATH}" "${identity_hash}" <<'PY'
import subprocess
import sys

try:
    result = subprocess.run(sys.argv[1:], stdin=subprocess.DEVNULL, timeout=30)
except subprocess.TimeoutExpired:
    print("iOS signing private-key preflight timed out after 30 seconds.", file=sys.stderr)
    sys.exit(1)
sys.exit(result.returncode if result.returncode >= 0 else 1)
PY
  echo "iOS signing identity installed temporarily."
}

install_profile() {
  : "${IOS_PROVISIONING_PROFILE:?Missing IOS_PROVISIONING_PROFILE}"
  [[ -f "${IOS_PROVISIONING_PROFILE}" ]] || {
    echo "iOS provisioning profile file does not exist." >&2
    exit 1
  }
  prepare_keychain
  local profile_plist profile_uuid profile_name profile_dir installed_profile
  profile_plist="${STATE_DIR}/profile.plist"
  security cms -D -k "${KEYCHAIN_PATH}" -i "${IOS_PROVISIONING_PROFILE}" > "${profile_plist}"
  profile_uuid="$(/usr/libexec/PlistBuddy -c 'Print :UUID' "${profile_plist}")"
  profile_name="$(/usr/libexec/PlistBuddy -c 'Print :Name' "${profile_plist}")"
  if [[ -z "${profile_uuid}" || -z "${profile_name}" ]]; then
    echo "Provisioning profile is missing UUID or Name." >&2
    exit 1
  fi
  profile_dir="${HOME}/Library/MobileDevice/Provisioning Profiles"
  installed_profile="${profile_dir}/${profile_uuid}.mobileprovision"
  mkdir -p "${profile_dir}"
  if [[ -f "${installed_profile}" ]]; then
    install -m 600 "${installed_profile}" "${PROFILE_BACKUP_FILE}"
  fi
  printf '%s\n' "${installed_profile}" > "${INSTALLED_PROFILE_PATH_FILE}"
  install -m 600 "${IOS_PROVISIONING_PROFILE}" "${installed_profile}"
  {
    printf 'IOS_PROFILE_NAME=%s\n' "${profile_name}"
    printf 'IOS_PROFILE_UUID=%s\n' "${profile_uuid}"
  } >> "${SIGNING_ENV_FILE}"
  rm -f "${profile_plist}"
  local identity_hash
  identity_hash="$(security find-identity -v -p codesigning "${KEYCHAIN_PATH}" | \
    awk -v team="${IOS_TEAM_ID:?Missing IOS_TEAM_ID}" '/"Apple Distribution:/ && index($0, "(" team ")\"") {print $2}')"
  [[ "${identity_hash}" =~ ^[0-9A-F]{40}$ ]] || {
    echo "Expected exactly one valid Apple Distribution identity for the selected Team before Archive." >&2
    return 1
  }
  # CMS 后验证真实 codesign 链与授权；只重签副本，保留原生挑战程序已授权的 ad-hoc CDHash。
  python3 - "${PREFLIGHT_BINARY}" "${KEYCHAIN_PATH}" "${identity_hash}" <<'PY'
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    with tempfile.TemporaryDirectory(prefix="codesign-preflight-", dir=Path(sys.argv[1]).parent) as directory:
        probe = Path(directory) / "probe"
        shutil.copy2(sys.argv[1], probe)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", sys.argv[3], "--keychain", sys.argv[2],
                        "--timestamp=none", str(probe)], stdin=subprocess.DEVNULL, timeout=30, check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(probe)],
                       stdin=subprocess.DEVNULL, timeout=30, check=True)
except subprocess.TimeoutExpired:
    print("iOS codesign preflight timed out after 30 seconds.", file=sys.stderr)
    sys.exit(1)
except (OSError, subprocess.CalledProcessError):
    print("iOS codesign preflight failed before Archive.", file=sys.stderr)
    sys.exit(1)
PY
  echo "iOS codesign preflight passed."
  echo "iOS provisioning profile installed temporarily."
  echo "Signing properties: ${SIGNING_ENV_FILE}"
}

install_signing() {
  install_certificate
  install_profile
}

verify_ipa() (
  # Jenkins Secret text 仅在受限临时文件中交给 OpenSSL，不进入命令参数或控制台。
  set +x
  umask 077
  local password_file="${IOS_DISTRIBUTION_P12_PASSWORD_FILE:-}" temporary_password=""
  if [[ -z "${password_file}" ]]; then
    : "${IOS_DISTRIBUTION_P12_PASSWORD:?Missing IOS_DISTRIBUTION_P12_PASSWORD}"
    mkdir -p "${STATE_DIR}"
    temporary_password="$(mktemp "${STATE_DIR}/verify-password.XXXXXX")"
    trap 'rm -f "${temporary_password}"' EXIT
    printf '%s' "${IOS_DISTRIBUTION_P12_PASSWORD}" > "${temporary_password}"
    password_file="${temporary_password}"
  fi
  local expected_version
  local pipeline_args=("--pipeline=${CI_IOS_PIPELINE:?Missing CI_IOS_PIPELINE}")
  if [[ "${CI_IOS_PIPELINE:-}" == test ]]; then
    pipeline_args+=("--channel=${CI_TEST_CHANNEL:?Missing CI_TEST_CHANNEL}")
  fi
  expected_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version_target"])' "${CI_OUTPUT_DIR:-build/ci-ios}/context.json")"
  python3 "${SCRIPT_DIR}/ios_verify_signature.py" \
    --config="${IOS_BRAND_CONFIG:-$(pwd)/ci/brands.json}" \
    --brand="${CI_BRAND:?Missing CI_BRAND}" \
    "${pipeline_args[@]}" \
    --ui-engine="${CI_UI_ENGINE:-cmp}" \
    --ipa="${IOS_IPA_PATH:?Missing IOS_IPA_PATH}" \
    --p12="${IOS_DISTRIBUTION_P12:?Missing IOS_DISTRIBUTION_P12}" \
    --password-file="${password_file}" \
    --expected-version="${expected_version}" \
    --expected-build-number="${IOS_BUILD_NUMBER:?Missing IOS_BUILD_NUMBER}" \
    --output="${CI_OUTPUT_DIR:-build/ci-ios}/signature-verification.json"
)

case "${1:-}" in
  prepare)
    trap cleanup_signing ERR
    prepare_keychain
    trap - ERR
    ;;
  install-certificate)
    trap cleanup_signing ERR
    install_certificate
    trap - ERR
    ;;
  install-profile)
    trap cleanup_signing ERR
    install_profile
    trap - ERR
    ;;
  install)
    trap cleanup_signing ERR
    install_signing
    trap - ERR
    ;;
  cleanup)
    cleanup_signing
    ;;
  verify-ipa)
    verify_ipa
    ;;
  *)
    usage
    exit 2
    ;;
esac
