#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="${APP_SOURCE_ROOT:-$(pwd)}"
STATE_DIR_INPUT="${IOS_SIGNING_STATE_DIR:-${REPO_ROOT}/build/ci-ios/signing}"
if [[ "${STATE_DIR_INPUT}" == /* ]]; then
  STATE_DIR="${STATE_DIR_INPUT}"
else
  STATE_DIR="$(pwd)/${STATE_DIR_INPUT}"
fi
PROFILE_OUTPUT_DIR="${STATE_DIR}/profile-output"
FASTLANE_RUNTIME_HOME="${STATE_DIR}/fastlane-home"
PROFILE_PATH_FILE="${STATE_DIR}/profile-path.txt"
SIGNING_ENV_FILE="${STATE_DIR}/signing.env"
REQUIRED_FASTLANE_VERSION="$(cat "${TOOL_ROOT}/delivery/.fastlane-version")"
FASTLANE_SCRIPT="${TOOL_ROOT}/delivery/fastlane.sh"
APPLE_CERTIFICATES_SCRIPT="${SCRIPT_DIR}/ios_apple_intermediate_certificates.sh"

usage() {
  echo "Usage: $0 {preflight|validate-device|list|sync|cleanup}"
}

fastlane_environment() (
  cd "${TOOL_ROOT}/delivery"
  local fastlane_gem_home original_home ruby_bin ruby_abi
  original_home="${HOME}"
  ruby_bin="${FASTLANE_RUBY:-$(brew --prefix ruby)/bin/ruby}"
  ruby_abi="$("${ruby_bin}" -rrbconfig -e 'puts RbConfig::CONFIG["ruby_version"]')"
  fastlane_gem_home="${FASTLANE_GEM_HOME:-${original_home}/.local/share/fastlane/${ruby_abi}}"
  HOME="${FASTLANE_RUNTIME_HOME}" \
  FASTLANE_RUBY="${ruby_bin}" \
  FASTLANE_GEM_HOME="${fastlane_gem_home}" \
  FASTLANE_SKIP_UPDATE_CHECK=1 \
  FASTLANE_HIDE_CHANGELOG=1 \
  FASTLANE_DISABLE_COLORS=1 \
  FASTLANE_OPT_OUT_USAGE=1 \
  CI=1 \
    "$@"
)

list_devices() {
  preflight
  for name in \
    ASC_API_KEY_PATH \
    ASC_API_KEY_ID \
    ASC_ISSUER_ID \
    IOS_TEAM_ID \
    IOS_DEVICES_OUTPUT_PATH; do
    require_value "${name}"
  done
  [[ -f "${ASC_API_KEY_PATH}" ]] || {
    echo "App Store Connect API key file does not exist." >&2
    exit 1
  }
  [[ ! -e "${FASTLANE_RUNTIME_HOME}" ]] || {
    echo "Refusing to reuse existing Fastlane runtime home." >&2
    exit 1
  }
  local output_path
  if [[ "${IOS_DEVICES_OUTPUT_PATH}" == /* ]]; then
    output_path="${IOS_DEVICES_OUTPUT_PATH}"
  else
    output_path="$(pwd)/${IOS_DEVICES_OUTPUT_PATH}"
  fi
  case "${output_path}" in
    "${REPO_ROOT}/build/ci-ios-devices/results/"*.json)
      ;;
    *)
      echo "Invalid iOS device inventory output path." >&2
      exit 1
      ;;
  esac
  umask 077
  mkdir -p "${FASTLANE_RUNTIME_HOME}"
  IOS_DEVICES_OUTPUT_PATH="${output_path}" \
    fastlane_environment bash "${FASTLANE_SCRIPT}" ios list_test_devices
}

require_value() {
  local name="$1"
  [[ -n "${!name:-}" ]] || {
    echo "Missing required environment: ${name}" >&2
    exit 1
  }
}

read_signing_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "${SIGNING_ENV_FILE}"
}

preflight() {
  local version
  version="$(bash "${FASTLANE_SCRIPT}" --version 2>/dev/null | sed -n 's/.*fastlane \([0-9][0-9.]*\)$/\1/p' | tail -1)"
  [[ "${version}" == "${REQUIRED_FASTLANE_VERSION}" ]] || {
    echo "fastlane ${REQUIRED_FASTLANE_VERSION} is required, found ${version:-unknown}." >&2
    exit 1
  }
  bash "${APPLE_CERTIFICATES_SCRIPT}" verify
  echo "fastlane: ${version}"
}

validate_device() {
  local name="${NEW_DEVICE_NAME:-}"
  local udid="${NEW_DEVICE_UDID:-}"
  if [[ -z "${name}" && -z "${udid}" ]]; then
    echo "No new iOS test device requested."
    return
  fi
  if [[ -z "${name}" || -z "${udid}" ]]; then
    echo "NEW_DEVICE_NAME and NEW_DEVICE_UDID must be provided together." >&2
    exit 2
  fi
  if [[ ${#name} -gt 80 || "${name}" == *[$'\r\n\t']* ]]; then
    echo "NEW_DEVICE_NAME must be 1-80 characters without control characters." >&2
    exit 2
  fi
  if [[ ! "${udid}" =~ ^([0-9A-Fa-f]{40}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16})$ ]]; then
    echo "NEW_DEVICE_UDID is not a supported iPhone UDID format." >&2
    exit 2
  fi
  echo "A new iOS test device will be registered before profile download."
}

sync_profile() {
  local distribution="${IOS_DISTRIBUTION:-}"
  case "${distribution}" in
    adhoc|appstore)
      ;;
    *)
      echo "IOS_DISTRIBUTION must be adhoc or appstore." >&2
      exit 2
      ;;
  esac
  preflight
  validate_device
  for name in \
    ASC_API_KEY_PATH \
    ASC_API_KEY_ID \
    ASC_ISSUER_ID \
    IOS_BUNDLE_ID \
    IOS_TEAM_ID \
    IOS_PROFILE_NAME \
    REGISTER_NEW_DEVICE; do
    require_value "${name}"
  done
  [[ -f "${ASC_API_KEY_PATH}" ]] || {
    echo "App Store Connect API key file does not exist." >&2
    exit 1
  }
  if [[ "${distribution}" == "appstore" && "${REGISTER_NEW_DEVICE}" != "false" ]]; then
    echo "App Store Profile download cannot register test devices." >&2
    exit 2
  fi
  [[ -f "${SIGNING_ENV_FILE}" ]] || {
    echo "Temporary signing keychain is not prepared." >&2
    exit 1
  }
  local keychain_path keychain_password_file
  keychain_path="$(read_signing_value IOS_KEYCHAIN_PATH)"
  keychain_password_file="$(read_signing_value IOS_KEYCHAIN_PASSWORD_FILE)"
  [[ -f "${keychain_path}" && -f "${keychain_password_file}" ]] || {
    echo "Temporary signing keychain state is incomplete." >&2
    exit 1
  }
  [[ ! -e "${PROFILE_OUTPUT_DIR}" && ! -e "${FASTLANE_RUNTIME_HOME}" ]] || {
    echo "Refusing to reuse existing iOS provisioning temporary output." >&2
    exit 1
  }

  umask 077
  mkdir -p \
    "${PROFILE_OUTPUT_DIR}" \
    "${FASTLANE_RUNTIME_HOME}/Library/Preferences"
  HOME="${FASTLANE_RUNTIME_HOME}" \
    security list-keychains -d user -s "${keychain_path}"
  HOME="${FASTLANE_RUNTIME_HOME}" \
    security default-keychain -d user -s "${keychain_path}"
  HOME="${FASTLANE_RUNTIME_HOME}" \
    security unlock-keychain \
      -p "$(<"${keychain_password_file}")" \
      "${keychain_path}"
  bash "${APPLE_CERTIFICATES_SCRIPT}" install "${keychain_path}"
  local lane profile_filename profile_label
  if [[ "${distribution}" == "adhoc" ]]; then
    lane="sync_adhoc_profile"
    profile_filename="current-adhoc.mobileprovision"
    profile_label="Ad Hoc"
  else
    lane="download_appstore_profile"
    profile_filename="current-appstore.mobileprovision"
    profile_label="App Store"
  fi
  IOS_PROFILE_OUTPUT_PATH="${PROFILE_OUTPUT_DIR}" \
    fastlane_environment bash "${FASTLANE_SCRIPT}" ios "${lane}"

  local profile="${PROFILE_OUTPUT_DIR}/${profile_filename}"
  [[ -f "${profile}" ]] || {
    echo "Fastlane did not download the expected ${profile_label} profile." >&2
    exit 1
  }

  local profile_plist application_identifier profile_bundle_id profile_team
  profile_plist="${STATE_DIR}/profile-validation.plist"
  security cms -D -k "${keychain_path}" -i "${profile}" > "${profile_plist}"
  application_identifier="$(/usr/libexec/PlistBuddy -c 'Print :Entitlements:application-identifier' "${profile_plist}")"
  profile_bundle_id="${application_identifier#*.}"
  profile_team="$(/usr/libexec/PlistBuddy -c 'Print :TeamIdentifier:0' "${profile_plist}")"
  if [[ "${distribution}" == "adhoc" ]]; then
    /usr/libexec/PlistBuddy -c 'Print :ProvisionedDevices:0' "${profile_plist}" >/dev/null
  elif /usr/libexec/PlistBuddy -c 'Print :ProvisionedDevices' "${profile_plist}" >/dev/null 2>&1 ||
    /usr/libexec/PlistBuddy -c 'Print :ProvisionsAllDevices' "${profile_plist}" >/dev/null 2>&1; then
    rm -f "${profile_plist}"
    echo "Downloaded profile is not an App Store distribution profile." >&2
    exit 1
  fi
  rm -f "${profile_plist}"
  [[ "${profile_bundle_id}" == "${IOS_BUNDLE_ID}" ]] || {
    echo "Downloaded profile Bundle ID does not match the selected brand." >&2
    exit 1
  }
  [[ "${profile_team}" == "${IOS_TEAM_ID}" ]] || {
    echo "Downloaded profile Developer Team does not match the selected brand." >&2
    exit 1
  }
  printf '%s\n' "${profile}" > "${PROFILE_PATH_FILE}"
  echo "Matching ${profile_label} profile downloaded for the selected iOS brand."
}

cleanup_profile() {
  rm -f "${PROFILE_PATH_FILE}" "${STATE_DIR}/profile-validation.plist"
  for path in "${PROFILE_OUTPUT_DIR}" "${FASTLANE_RUNTIME_HOME}"; do
    case "${path}" in
      "${STATE_DIR}/profile-output"|"${STATE_DIR}/fastlane-home")
        if [[ -d "${path}" ]]; then
          find "${path}" -depth -delete
        fi
        ;;
      *)
        echo "Refusing to clean unexpected iOS provisioning path: ${path}" >&2
        exit 1
        ;;
    esac
  done
  rmdir "${STATE_DIR}" >/dev/null 2>&1 || true
  echo "iOS provisioning temporary state cleaned."
}

case "${1:-}" in
  preflight)
    preflight
    ;;
  validate-device)
    validate_device
    ;;
  list)
    list_devices
    ;;
  sync)
    sync_profile
    ;;
  cleanup)
    cleanup_profile
    ;;
  *)
    usage
    exit 2
    ;;
esac
