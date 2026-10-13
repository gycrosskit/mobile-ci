#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CI_TOOL_ROOT="$(cd "${CI_TOOL_ROOT:-${SCRIPT_DIR}/..}" && pwd)"
REPO_ROOT="$(cd "${APP_SOURCE_ROOT:-${PWD}}" && pwd)"
export CI_TOOL_ROOT APP_SOURCE_ROOT="${REPO_ROOT}"
export PYTHONPATH="${CI_TOOL_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
contract="$(python3 "${CI_TOOL_ROOT}/project.py" ios "${CI_UI_ENGINE-cmp}")" || exit 2
contract_values=()
while IFS= read -r line; do contract_values+=("${line}"); done <<< "${contract}"
[[ ${#contract_values[@]} -eq 7 ]] || exit 2
CONTAINER_KIND="${contract_values[0]}"; WORKSPACE_PATH="${contract_values[1]}"
DESTINATION_KIND="${contract_values[2]}"; DESTINATION_PATH="${contract_values[3]}"
IOS_DEFAULT_SCHEME="${contract_values[4]}"; IOS_CONFIGURATION="${contract_values[5]}"; IOS_ADAPTER="${contract_values[6]}"
if [[ "${IOS_ADAPTER}" == "sxmqlive" && ! -f "${REPO_ROOT}/gradlew" ]]; then
  echo "APP_SOURCE_ROOT sxmqlive iOS adapter requires application gradlew." >&2; exit 2
fi
OUTPUT_DIR="${CI_OUTPUT_DIR:-${REPO_ROOT}/build/ci-ios}"
IOS_XCODE_GRADLE_OPTS="${IOS_XCODE_GRADLE_OPTS:--Dorg.gradle.jvmargs=-Xmx6144m -Dorg.gradle.workers.max=1 -Dorg.gradle.parallel=false}"

usage() {
  cat <<'EOF'
Usage:
  ios_ci.sh preflight [scheme]
  ios_ci.sh build-unsigned <dev|prod> <scheme> <xcconfig>
  ios_ci.sh archive <dev|prod> <adhoc|appstore> <scheme> <xcconfig> <team-id> <build-number> <profile-name> <keychain-path>
  ios_ci.sh export <adhoc|appstore> <team-id> <bundle-id> <profile-name> <archive-path> <export-dir>
  ios_ci.sh find-ipa <export-dir>
  ios_ci.sh package-artifacts <archive-path>
  ios_ci.sh upload-app-store <ipa-path>
EOF
}

require_file() {
  [[ -f "$1" ]] || {
    echo "Required file does not exist: $1" >&2
    exit 1
  }
}

resolve_xcconfig() {
  local relative_path="$1"
  python3 "${CI_TOOL_ROOT}/project.py" xcconfig "${relative_path}"
}

run_xcodebuild_with_gradle_limits() {
  # Xcode 的 shared framework Phase 会调用 Gradle；只在本次调用限制并发并提高堆，不改变全局配置。
  if [[ "${IOS_ADAPTER}" == "sxmqlive" ]]; then
    GRADLE_OPTS="${IOS_XCODE_GRADLE_OPTS}" CI_GRADLE_PROFILE="${CI_GRADLE_PROFILE:-}" xcodebuild "$@"
  else
    xcodebuild "$@"
  fi
}

preflight() {
  local scheme="${1:-${IOS_DEFAULT_SCHEME}}"
  [[ "${IOS_ADAPTER}" == "sxmqlive" || "${scheme}" == "${IOS_DEFAULT_SCHEME}" ]] || { echo "Scheme differs from trusted profile." >&2; exit 2; }
  [[ "$(uname -s)" == "Darwin" ]] || {
    echo "iOS builds require a macOS agent." >&2
    exit 1
  }
  local required_tools=(xcodebuild xcrun security codesign ditto)
  [[ "${IOS_ADAPTER}" != "sxmqlive" ]] || required_tools+=(pod java)
  for tool in "${required_tools[@]}"; do
    command -v "${tool}" >/dev/null || {
      echo "Missing iOS build tool: ${tool}" >&2
      exit 1
    }
  done
  [[ -d "${WORKSPACE_PATH}" ]] || {
    echo "Missing Xcode workspace: ${WORKSPACE_PATH}" >&2
    exit 1
  }
  local installed_sdks iphoneos_sdk_path iphoneos_sdk_version destinations
  installed_sdks="$(xcodebuild -showsdks)" || {
    echo "Unable to query installed Xcode SDKs." >&2
    exit 1
  }
  [[ "${installed_sdks}" == *"-sdk iphoneos"* ]] || {
    echo "Missing iPhoneOS SDK. Install the iOS Platform required by the active Xcode before running iOS CI." >&2
    exit 1
  }
  iphoneos_sdk_path="$(xcrun --sdk iphoneos --show-sdk-path 2>/dev/null)" || {
    echo "The active Xcode cannot resolve an iPhoneOS SDK." >&2
    exit 1
  }
  [[ -d "${iphoneos_sdk_path}" ]] || {
    echo "Resolved iPhoneOS SDK does not exist: ${iphoneos_sdk_path}" >&2
    exit 1
  }
  iphoneos_sdk_version="$(xcrun --sdk iphoneos --show-sdk-version 2>/dev/null)" || {
    echo "The active Xcode cannot report the iPhoneOS SDK version." >&2
    exit 1
  }
  destinations="$(xcodebuild -showdestinations "${DESTINATION_KIND}" "${DESTINATION_PATH}" -scheme "${scheme}" 2>&1)" || {
    echo "Cannot query Xcode destinations for ${scheme}." >&2
    exit 1
  }
  if ! printf '%s\n' "${destinations}" | awk '
    /Available destinations/ { available = 1; next }
    /Ineligible destinations/ { available = 0 }
    available && /platform:iOS,/ && /name:Any iOS Device/ { found = 1 }
    END { exit !found }
  '; then
    echo "Generic iOS device destination is unavailable for ${scheme} under $(id -un). Check the iOS Platform in Xcode for this agent account." >&2
    exit 1
  fi
  echo "Xcode: $(xcodebuild -version | tr '\n' ' ')"
  echo "iPhoneOS SDK: ${iphoneos_sdk_version} (${iphoneos_sdk_path})"
  if [[ "${IOS_ADAPTER}" == "sxmqlive" ]]; then
    echo "CocoaPods: $(pod --version)"
    echo "Java: $(java -version 2>&1 | head -1)"
  fi
  echo "iOS macOS agent preflight passed: ${scheme} generic iOS device is available."
}

build_unsigned() {
  local environment="$1"
  local scheme="$2"
  [[ "${IOS_ADAPTER}" == "sxmqlive" || "${scheme}" == "${IOS_DEFAULT_SCHEME}" ]] || { echo "Scheme differs from trusted profile." >&2; exit 2; }
  local xcconfig
  xcconfig="$(resolve_xcconfig "$3")"
  [[ "${environment}" == "dev" || "${environment}" == "prod" ]] || {
    echo "Unsupported iOS build environment: ${environment}" >&2
    exit 2
  }
  run_xcodebuild_with_gradle_limits \
    "${CONTAINER_KIND}" "${WORKSPACE_PATH}" \
    -scheme "${scheme}" \
    -configuration "${IOS_CONFIGURATION}" \
    -xcconfig "${xcconfig}" \
    -destination 'generic/platform=iOS' \
    -showBuildTimingSummary \
    build CODE_SIGNING_ALLOWED=NO
}

archive_app() {
  local environment="$1"
  local distribution="$2"
  local scheme="$3"
  [[ "${IOS_ADAPTER}" == "sxmqlive" || "${scheme}" == "${IOS_DEFAULT_SCHEME}" ]] || { echo "Scheme differs from trusted profile." >&2; exit 2; }
  local xcconfig
  xcconfig="$(resolve_xcconfig "$4")"
  local team_id="$5"
  local build_number="$6"
  local profile_name="$7"
  local keychain_path="$8"
  [[ "${environment}" == "dev" || "${environment}" == "prod" ]] || {
    echo "Unsupported iOS build environment: ${environment}" >&2
    exit 2
  }
  [[ "${distribution}" == "adhoc" || "${distribution}" == "appstore" ]] || {
    echo "Unsupported iOS distribution: ${distribution}" >&2
    exit 2
  }
  [[ "${build_number}" =~ ^[0-9]+$ ]] || {
    echo "iOS build number must be a positive integer: ${build_number}" >&2
    exit 2
  }
  require_file "${keychain_path}"
  local archive_path="${OUTPUT_DIR}/archive/${scheme}-${environment}-${distribution}.xcarchive"
  if [[ -e "${archive_path}" ]]; then
    echo "Refusing to reuse an existing archive path: ${archive_path}" >&2
    exit 1
  fi
  mkdir -p "$(dirname "${archive_path}")"

  CI_GRADLE_PROFILE=1 run_xcodebuild_with_gradle_limits \
    "${CONTAINER_KIND}" "${WORKSPACE_PATH}" \
    -scheme "${scheme}" \
    -configuration "${IOS_CONFIGURATION}" \
    -xcconfig "${xcconfig}" \
    -destination 'generic/platform=iOS' \
    -archivePath "${archive_path}" \
    -onlyUsePackageVersionsFromResolvedFile \
    -showBuildTimingSummary \
    archive \
    CURRENT_PROJECT_VERSION="${build_number}" \
    CODE_SIGN_STYLE=Manual \
    DEVELOPMENT_TEAM="${team_id}" \
    CODE_SIGN_IDENTITY='Apple Distribution' \
    PROVISIONING_PROFILE_SPECIFIER="${profile_name}" \
    "OTHER_CODE_SIGN_FLAGS=--keychain ${keychain_path}"
  echo "${archive_path}"
}

export_archive() {
  local distribution="$1"
  local team_id="$2"
  local bundle_id="$3"
  local profile_name="$4"
  local archive_path="$5"
  local export_dir="$6"
  [[ "${distribution}" == "adhoc" || "${distribution}" == "appstore" ]] || {
    echo "Unsupported iOS distribution: ${distribution}" >&2
    exit 2
  }
  [[ -d "${archive_path}" ]] || {
    echo "Archive does not exist: ${archive_path}" >&2
    exit 1
  }
  if [[ -e "${export_dir}" ]]; then
    echo "Refusing to reuse an existing export directory: ${export_dir}" >&2
    exit 1
  fi
  mkdir -p "${export_dir}"
  local options="${OUTPUT_DIR}/ExportOptions-${distribution}.plist"
  if [[ -e "${options}" ]]; then
    echo "Refusing to reuse existing export options: ${options}" >&2
    exit 1
  fi
  local method
  method="release-testing"
  if [[ "${distribution}" == "appstore" ]]; then
    method="app-store-connect"
  fi
  /usr/libexec/PlistBuddy -c "Add :method string ${method}" "${options}"
  /usr/libexec/PlistBuddy -c 'Add :destination string export' "${options}"
  /usr/libexec/PlistBuddy -c 'Add :signingStyle string manual' "${options}"
  /usr/libexec/PlistBuddy -c "Add :teamID string ${team_id}" "${options}"
  /usr/libexec/PlistBuddy -c 'Add :signingCertificate string Apple Distribution' "${options}"
  /usr/libexec/PlistBuddy -c 'Add :manageAppVersionAndBuildNumber bool false' "${options}"
  /usr/libexec/PlistBuddy -c 'Add :stripSwiftSymbols bool true' "${options}"
  /usr/libexec/PlistBuddy -c 'Add :uploadSymbols bool true' "${options}"
  /usr/libexec/PlistBuddy -c 'Add :provisioningProfiles dict' "${options}"
  /usr/libexec/PlistBuddy \
    -c "Add :provisioningProfiles:${bundle_id} string ${profile_name}" \
    "${options}"
  xcodebuild \
    -exportArchive \
    -archivePath "${archive_path}" \
    -exportPath "${export_dir}" \
    -exportOptionsPlist "${options}"
}

find_ipa() {
  local export_dir="$1"
  [[ -d "${export_dir}" ]] || {
    echo "Export directory does not exist: ${export_dir}" >&2
    exit 1
  }
  local matches=()
  while IFS= read -r path; do
    matches+=("${path}")
  done < <(find "${export_dir}" -maxdepth 1 -type f -name '*.ipa' -print | sort)
  if [[ ${#matches[@]} -ne 1 ]]; then
    echo "Expected exactly one IPA in ${export_dir}, found ${#matches[@]}." >&2
    exit 1
  fi
  echo "${matches[0]}"
}

package_artifacts() {
  local archive_path="$1"
  [[ -d "${archive_path}" ]] || {
    echo "Archive does not exist: ${archive_path}" >&2
    exit 1
  }
  local output="${OUTPUT_DIR}/$(basename "${archive_path}").zip"
  ditto -c -k --sequesterRsrc --keepParent "${archive_path}" "${output}"
  echo "${output}"
}

upload_app_store() {
  local ipa_path="$1"
  require_file "${ipa_path}"
  : "${ASC_API_KEY_PATH:?Missing ASC_API_KEY_PATH}"
  : "${ASC_API_KEY_ID:?Missing ASC_API_KEY_ID}"
  : "${ASC_API_ISSUER_ID:?Missing ASC_API_ISSUER_ID}"
  require_file "${ASC_API_KEY_PATH}"
  xcrun altool \
    --validate-app \
    -f "${ipa_path}" \
    --api-key "${ASC_API_KEY_ID}" \
    --api-issuer "${ASC_API_ISSUER_ID}" \
    --p8-file-path "${ASC_API_KEY_PATH}" \
    --output-format json
  xcrun altool \
    --upload-app \
    -f "${ipa_path}" \
    --api-key "${ASC_API_KEY_ID}" \
    --api-issuer "${ASC_API_ISSUER_ID}" \
    --p8-file-path "${ASC_API_KEY_PATH}" \
    --output-format json
}

case "${1:-}" in
  preflight)
    [[ $# -le 2 ]] || { usage; exit 2; }
    preflight "${2:-}"
    ;;
  build-unsigned)
    [[ $# -eq 4 ]] || { usage; exit 2; }
    build_unsigned "$2" "$3" "$4"
    ;;
  archive)
    [[ $# -eq 9 ]] || { usage; exit 2; }
    archive_app "$2" "$3" "$4" "$5" "$6" "$7" "$8" "$9"
    ;;
  export)
    [[ $# -eq 7 ]] || { usage; exit 2; }
    export_archive "$2" "$3" "$4" "$5" "$6" "$7"
    ;;
  find-ipa)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    find_ipa "$2"
    ;;
  package-artifacts)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    package_artifacts "$2"
    ;;
  upload-app-store)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    upload_app_store "$2"
    ;;
  *)
    usage
    exit 2
    ;;
esac
