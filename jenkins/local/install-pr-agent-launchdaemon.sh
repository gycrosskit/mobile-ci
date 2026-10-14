#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TOOLCHAIN_LOCK_CLI="${REPO_ROOT}/toolchains.py"
REQUIRED_FASTLANE_VERSION="$(python3 "${TOOLCHAIN_LOCK_CLI}" version fastlane)"
REQUIRED_JDK_VERSION="$(python3 "${TOOLCHAIN_LOCK_CLI}" version jdk)"
REQUIRED_XCODE_VERSION="$(python3 "${TOOLCHAIN_LOCK_CLI}" version xcode)"
ACTION="check"
ACCEPT_ANDROID_LICENSES="false"
SERVICE_USER="${JENKINS_PR_SERVICE_USER:-_sxmqlive_pr}"
SERVICE_HOME="${JENKINS_PR_SERVICE_HOME:-/Users/Shared/sxmqlive-jenkins-pr}"
INSTALL_ROOT="${JENKINS_PR_INSTALL_ROOT:-${SERVICE_HOME}/runtime-agent}"
AGENT_WORK_DIR="${JENKINS_PR_AGENT_WORK_DIR:-${SERVICE_HOME}/agent}"
GRADLE_USER_HOME_VALUE="${JENKINS_PR_GRADLE_USER_HOME:-${SERVICE_HOME}/gradle-cache}"
ANDROID_SDK_ROOT_VALUE="${JENKINS_PR_ANDROID_SDK_ROOT:-${SERVICE_HOME}/android-sdk}"
JENKINS_URL_VALUE="${JENKINS_URL:-http://127.0.0.1:18080}"
AGENT_SECRET_SOURCE="${JENKINS_PR_AGENT_SECRET_FILE:-}"
PLIST_SOURCE="${SCRIPT_DIR}/com.sxmqlive.jenkins-pr-agent.plist.template"
GITEE_KNOWN_HOSTS_SOURCE="${SCRIPT_DIR}/pr-agent/gitee-known-hosts"
PLIST_TARGET="/Library/LaunchDaemons/com.sxmqlive.jenkins-pr-agent.plist"
LAUNCHD_LABEL="com.sxmqlive.jenkins-pr-agent"
REQUIRED_ANDROID_PLATFORM="android-$(python3 "${TOOLCHAIN_LOCK_CLI}" version androidSDK)"
REQUIRED_ANDROID_BUILD_TOOLS="$(python3 "${TOOLCHAIN_LOCK_CLI}" version androidBuildTools)"
REQUIRED_ANDROID_NDK="$(python3 "${TOOLCHAIN_LOCK_CLI}" version androidNDK)"

usage() {
  cat <<'EOF'
Usage:
  install-pr-agent-launchdaemon.sh check
  install-pr-agent-launchdaemon.sh install [--accept-android-licenses]

The install action creates a hidden macOS service account, installs an isolated
Android SDK and Fastlane, installs the explicitly provided mac-build inbound-agent Secret, and
installs a system LaunchDaemon and the pinned public Apple WWDRG3 CA. It never imports DWS, private signing material, or user keychain
state.

Required for install:
  JENKINS_PR_AGENT_SECRET_FILE  Absolute path to the mac-build Agent Secret.

JENKINS_URL defaults to the loopback endpoint created by the remote Controller
SSH tunnel.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    check|install)
      ACTION="$1"
      ;;
    --accept-android-licenses)
      ACCEPT_ANDROID_LICENSES="true"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "The Mac build Agent installer only supports macOS." >&2
  exit 1
fi
if [[ "${ACTION}" == "check" && "${ACCEPT_ANDROID_LICENSES}" == "true" ]]; then
  echo "--accept-android-licenses is only valid with install." >&2
  exit 2
fi
if [[ ! "${SERVICE_USER}" =~ ^_[a-z0-9_]+$ ]]; then
  echo "Invalid service user: ${SERVICE_USER}" >&2
  exit 2
fi
for value in \
  "${SERVICE_HOME}" \
  "${INSTALL_ROOT}" \
  "${AGENT_WORK_DIR}" \
  "${GRADLE_USER_HOME_VALUE}" \
  "${ANDROID_SDK_ROOT_VALUE}" \
  "${JENKINS_URL_VALUE}"; do
  if [[ "${value}" == *'#'* || "${value}" == *'&'* ]]; then
    echo "Unsupported character in configured path or URL: ${value}" >&2
    exit 2
  fi
done
if [[ ! "${JENKINS_URL_VALUE}" =~ ^https?://[^[:space:]]+$ ]]; then
  echo "Invalid Jenkins URL: ${JENKINS_URL_VALUE}" >&2
  exit 2
fi
if [[ "${ACTION}" == "install" && ( "${AGENT_SECRET_SOURCE}" != /* || ! -s "${AGENT_SECRET_SOURCE}" ) ]]; then
  echo "JENKINS_PR_AGENT_SECRET_FILE must be an existing non-empty absolute path." >&2
  exit 2
fi

resolve_executable() {
  local name="$1" candidate
  for candidate in "/opt/homebrew/bin/${name}" "/usr/local/bin/${name}" "/usr/bin/${name}"; do
    if [[ -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  echo "Missing required command: ${name}" >&2
  return 1
}

BREW_EXECUTABLE="$(resolve_executable brew)"
BREW_PREFIX="$(${BREW_EXECUTABLE} --prefix)"
SDKMANAGER_EXECUTABLE="$(resolve_executable sdkmanager)"
POD_EXECUTABLE="$(resolve_executable pod)"
JAVA_HOME_VALUE="${JENKINS_PR_JAVA_HOME:-${BREW_PREFIX}/opt/openjdk@${REQUIRED_JDK_VERSION}/libexec/openjdk.jdk/Contents/Home}"
DEVELOPER_DIR_VALUE="${DEVELOPER_DIR:-$(/usr/bin/xcode-select -p 2>/dev/null || true)}"
RUBY_BIN="$(${BREW_EXECUTABLE} --prefix ruby)/bin/ruby"
TOOL_PATH="${RUBY_BIN%/*}:${BREW_PREFIX}/bin:${JAVA_HOME_VALUE}/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export DEVELOPER_DIR="${DEVELOPER_DIR_VALUE}"

check_host_tools() {
  local iphoneos_sdk_path xcode_version
  [[ -x "${JAVA_HOME_VALUE}/bin/java" ]] || {
    echo "JDK ${REQUIRED_JDK_VERSION} is unavailable under ${JAVA_HOME_VALUE}." >&2
    return 1
  }
  "${JAVA_HOME_VALUE}/bin/java" -version 2>&1 | head -1 | grep -q "\"${REQUIRED_JDK_VERSION}\." || {
    echo "JDK ${REQUIRED_JDK_VERSION} is required." >&2
    return 1
  }
  JAVA_HOME="${JAVA_HOME_VALUE}" PATH="${TOOL_PATH}" "${SDKMANAGER_EXECUTABLE}" --version >/dev/null
  # 只读版本检查允许管理员启动安装器；实际 Pods 始终在服务账号下运行。
  "${POD_EXECUTABLE}" --allow-root --version >/dev/null
  [[ -d "${DEVELOPER_DIR_VALUE}" && -x "${RUBY_BIN}" ]] || {
    echo "Xcode developer directory and Homebrew Ruby are required." >&2
    return 1
  }
  /usr/bin/xcodebuild -checkFirstLaunchStatus >/dev/null
  xcode_version="$(/usr/bin/xcodebuild -version | /usr/bin/sed -n 's/^Xcode //p')"
  [[ "${xcode_version}" == "${REQUIRED_XCODE_VERSION}" ]] || {
    echo "Xcode ${REQUIRED_XCODE_VERSION} is required, found ${xcode_version:-unknown}." >&2
    return 1
  }
  iphoneos_sdk_path="$(/usr/bin/xcrun --sdk iphoneos --show-sdk-path 2>/dev/null)" || {
    echo "The active Xcode does not provide an iPhoneOS SDK." >&2
    return 1
  }
  [[ -d "${iphoneos_sdk_path}" ]] || {
    echo "Resolved iPhoneOS SDK does not exist: ${iphoneos_sdk_path}" >&2
    return 1
  }
  [[ -f "${PLIST_SOURCE}" \
    && -f "${GITEE_KNOWN_HOSTS_SOURCE}" \
    && -x "${SCRIPT_DIR}/run-agent.sh" ]] || {
    echo "Mac build Agent templates are incomplete." >&2
    return 1
  }
  /usr/bin/ssh-keygen -F gitee.com -f "${GITEE_KNOWN_HOSTS_SOURCE}" \
    | /usr/bin/ssh-keygen -lf - \
    | /usr/bin/grep --fixed-strings \
      'SHA256:+ULzij2u99B9eWYFTw1Q4ErYG/aepHLbu96PAUCoV88' >/dev/null
}

service_fastlane() {
  /usr/bin/sudo -u "${SERVICE_USER}" /usr/bin/env \
    HOME="${SERVICE_HOME}" PATH="${TOOL_PATH}" FASTLANE_RUBY="${RUBY_BIN}" \
    FASTLANE_GEM_HOME="${FASTLANE_GEM_HOME_VALUE}" \
    bash "${REPO_ROOT}/delivery/fastlane.sh" "$@"
}

check_fastlane() {
  local version
  version="$(service_fastlane --version 2>/dev/null | /usr/bin/sed -n 's/.*fastlane \([0-9][0-9.]*\)$/\1/p' | /usr/bin/tail -1)" || true
  [[ "${version}" == "${REQUIRED_FASTLANE_VERSION}" ]] || {
    echo "Service account fastlane ${REQUIRED_FASTLANE_VERSION} is required, found ${version:-unknown}." >&2
    return 1
  }
}

android_packages_present() {
  /usr/bin/sudo -n -u "${SERVICE_USER}" /bin/test \
    -d "${ANDROID_SDK_ROOT_VALUE}/platform-tools" \
    && /usr/bin/sudo -n -u "${SERVICE_USER}" /bin/test \
      -d "${ANDROID_SDK_ROOT_VALUE}/platforms/${REQUIRED_ANDROID_PLATFORM}" \
    && /usr/bin/sudo -n -u "${SERVICE_USER}" /bin/test \
      -d "${ANDROID_SDK_ROOT_VALUE}/build-tools/${REQUIRED_ANDROID_BUILD_TOOLS}" \
    && /usr/bin/sudo -n -u "${SERVICE_USER}" /bin/test \
      -d "${ANDROID_SDK_ROOT_VALUE}/ndk/${REQUIRED_ANDROID_NDK}"
}

check_runtime() {
  if ! /usr/bin/id "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "Mac build Agent is not installed; host prerequisites are ready."
    return 0
  fi
  [[ -f "${PLIST_TARGET}" ]] || {
    echo "Mac build Agent LaunchDaemon is missing." >&2
    return 1
  }
  bash "${REPO_ROOT}/signing/ios_apple_intermediate_certificates.sh" check-system
  /bin/launchctl print "system/${LAUNCHD_LABEL}" >/dev/null
  [[ "$(/usr/bin/plutil -extract EnvironmentVariables.JENKINS_AGENT_NAME raw -o - "${PLIST_TARGET}")" == "mac-build" \
    && "$(/usr/bin/plutil -extract ProcessType raw -o - "${PLIST_TARGET}")" == "Standard" \
    && "$(/usr/bin/plutil -extract SessionCreate raw -o - "${PLIST_TARGET}")" == "true" ]] || {
    echo "Reinstall the service LaunchDaemon for mac-build with Standard ProcessType and SessionCreate." >&2
    return 1
  }
  if /usr/bin/sudo -n true 2>/dev/null; then
    android_packages_present || {
      echo "Mac build Agent Android SDK is incomplete." >&2
      return 1
    }
    /usr/bin/sudo -n -u "${SERVICE_USER}" /bin/test \
      -s "${INSTALL_ROOT}/runtime/agent.secret" || {
      echo "Mac build Agent Secret is missing." >&2
      return 1
    }
    check_fastlane
    echo "Mac build Agent service, Android SDK and Fastlane are ready."
  else
    echo "Mac build Agent service is registered; run install again or use sudo check for protected SDK and Fastlane details."
  fi
}

create_service_user() {
  local existing_home next_uid
  if /usr/bin/id "${SERVICE_USER}" >/dev/null 2>&1; then
    existing_home="$(/usr/bin/dscl . -read "/Users/${SERVICE_USER}" NFSHomeDirectory | /usr/bin/awk '{print $2}')"
    [[ "${existing_home}" == "${SERVICE_HOME}" ]] || {
      echo "Existing ${SERVICE_USER} home is ${existing_home}, expected ${SERVICE_HOME}." >&2
      exit 1
    }
    return
  fi
  next_uid="$(/usr/bin/dscl . -list /Users UniqueID | /usr/bin/awk '$2 >= 501 && $2 < 60000 { if ($2 > max) max = $2 } END { print (max ? max : 500) + 1 }')"
  /usr/bin/sudo /usr/bin/dscl . -create "/Users/${SERVICE_USER}"
  /usr/bin/sudo /usr/bin/dscl . -create "/Users/${SERVICE_USER}" RealName "sxmqlive Jenkins Mac Build Agent"
  /usr/bin/sudo /usr/bin/dscl . -create "/Users/${SERVICE_USER}" UniqueID "${next_uid}"
  /usr/bin/sudo /usr/bin/dscl . -create "/Users/${SERVICE_USER}" PrimaryGroupID 20
  /usr/bin/sudo /usr/bin/dscl . -create "/Users/${SERVICE_USER}" NFSHomeDirectory "${SERVICE_HOME}"
  /usr/bin/sudo /usr/bin/dscl . -create "/Users/${SERVICE_USER}" UserShell /usr/bin/false
  /usr/bin/sudo /usr/bin/dscl . -create "/Users/${SERVICE_USER}" IsHidden 1
  /usr/bin/sudo /usr/bin/dscl . -create "/Users/${SERVICE_USER}" AuthenticationAuthority ";DisabledUser;"
}

install_android_sdk() {
  if android_packages_present; then
    return
  fi
  if [[ "${ACCEPT_ANDROID_LICENSES}" != "true" ]]; then
    echo "The isolated Android SDK is incomplete." >&2
    echo "Re-run with --accept-android-licenses after reviewing the Android SDK licenses." >&2
    exit 2
  fi
  (
    set +o pipefail
    /usr/bin/yes | /usr/bin/sudo -u "${SERVICE_USER}" /usr/bin/env \
      HOME="${SERVICE_HOME}" \
      JAVA_HOME="${JAVA_HOME_VALUE}" \
      PATH="${TOOL_PATH}" \
      "${SDKMANAGER_EXECUTABLE}" \
      --sdk_root="${ANDROID_SDK_ROOT_VALUE}" \
      --licenses >/dev/null
  )
  /usr/bin/sudo -u "${SERVICE_USER}" /usr/bin/env \
    HOME="${SERVICE_HOME}" \
    JAVA_HOME="${JAVA_HOME_VALUE}" \
    PATH="${TOOL_PATH}" \
    "${SDKMANAGER_EXECUTABLE}" \
    --sdk_root="${ANDROID_SDK_ROOT_VALUE}" \
    "platform-tools" \
    "platforms;${REQUIRED_ANDROID_PLATFORM}" \
    "build-tools;${REQUIRED_ANDROID_BUILD_TOOLS}" \
    "ndk;${REQUIRED_ANDROID_NDK}"
}

install_runtime() {
  local temporary_dir rendered_plist
  temporary_dir="$(/usr/bin/mktemp -d /tmp/sxmqlive-pr-agent.XXXXXX)"
  trap "/bin/rm -rf '${temporary_dir}'" EXIT
  rendered_plist="${temporary_dir}/com.sxmqlive.jenkins-pr-agent.plist"

  /usr/bin/sudo /usr/bin/install -d -m 700 -o "${SERVICE_USER}" -g staff \
    "${SERVICE_HOME}" \
    "${INSTALL_ROOT}" \
    "${INSTALL_ROOT}/runtime" \
    "${INSTALL_ROOT}/logs" \
    "${SERVICE_HOME}/.ssh" \
    "${AGENT_WORK_DIR}" \
    "${GRADLE_USER_HOME_VALUE}" \
    "${ANDROID_SDK_ROOT_VALUE}"
  /usr/bin/sudo /usr/bin/install -m 755 -o "${SERVICE_USER}" -g staff \
    "${SCRIPT_DIR}/run-agent.sh" \
    "${INSTALL_ROOT}/run-agent.sh"
  /usr/bin/sudo /usr/bin/install -m 600 -o "${SERVICE_USER}" -g staff \
    "${AGENT_SECRET_SOURCE}" \
    "${INSTALL_ROOT}/runtime/agent.secret"
  /usr/bin/sudo /usr/bin/install -m 600 -o "${SERVICE_USER}" -g staff \
    "${GITEE_KNOWN_HOSTS_SOURCE}" \
    "${SERVICE_HOME}/.ssh/known_hosts"

  /usr/bin/sed \
    -e "s#__SERVICE_USER__#${SERVICE_USER}#g" \
    -e "s#__SERVICE_HOME__#${SERVICE_HOME}#g" \
    -e "s#__INSTALL_ROOT__#${INSTALL_ROOT}#g" \
    -e "s#__AGENT_WORK_DIR__#${AGENT_WORK_DIR}#g" \
    -e "s#__ANDROID_SDK_ROOT__#${ANDROID_SDK_ROOT_VALUE}#g" \
    -e "s#__GRADLE_USER_HOME__#${GRADLE_USER_HOME_VALUE}#g" \
    -e "s#__JENKINS_URL__#${JENKINS_URL_VALUE}#g" \
    -e "s#__JAVA_HOME__#${JAVA_HOME_VALUE}#g" \
    -e "s#__DEVELOPER_DIR__#${DEVELOPER_DIR_VALUE}#g" \
    -e "s#__RUBY_BIN__#${RUBY_BIN}#g" \
    -e "s#__FASTLANE_GEM_HOME__#${FASTLANE_GEM_HOME_VALUE}#g" \
    -e "s#__TOOL_PATH__#${TOOL_PATH}#g" \
    -e "s#__LOG_DIR__#${INSTALL_ROOT}/logs#g" \
    "${PLIST_SOURCE}" > "${rendered_plist}"
  /usr/bin/plutil -lint "${rendered_plist}" >/dev/null
  /usr/bin/sudo /bin/launchctl bootout "system/${LAUNCHD_LABEL}" >/dev/null 2>&1 || true
  /usr/bin/sudo /usr/bin/install -m 644 -o root -g wheel "${rendered_plist}" "${PLIST_TARGET}"
  if ! /usr/bin/sudo /bin/launchctl bootstrap system "${PLIST_TARGET}"; then
    # bootout 返回后旧服务可能仍在退出，给 launchd 一次短暂收敛时间。
    /bin/sleep 2
    /usr/bin/sudo /bin/launchctl bootstrap system "${PLIST_TARGET}"
  fi
}

check_host_tools
FASTLANE_GEM_HOME_VALUE="${SERVICE_HOME}/.local/share/fastlane/$("${RUBY_BIN}" -rrbconfig -e 'puts RbConfig::CONFIG["ruby_version"]')"
for value in "${DEVELOPER_DIR_VALUE}" "${JAVA_HOME_VALUE}" "${RUBY_BIN}" "${TOOL_PATH}"; do
  if [[ "${value}" == *'#'* || "${value}" == *'&'* ]]; then
    echo "Unsupported character in configured toolchain path: ${value}" >&2
    exit 2
  fi
done
if [[ "${ACTION}" == "check" ]]; then
  check_runtime
  exit 0
fi
/usr/bin/sudo -v
/usr/bin/sudo /bin/bash "${REPO_ROOT}/signing/ios_apple_intermediate_certificates.sh" install-system
create_service_user
/usr/bin/sudo /usr/bin/install -d -m 700 -o "${SERVICE_USER}" -g staff \
  "${SERVICE_HOME}" \
  "${INSTALL_ROOT}" \
  "${AGENT_WORK_DIR}" \
  "${GRADLE_USER_HOME_VALUE}" \
  "${ANDROID_SDK_ROOT_VALUE}"
install_android_sdk
if ! check_fastlane; then
  service_fastlane --install
  check_fastlane
fi
install_runtime
check_runtime
