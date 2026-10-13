#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BREWFILE="${SCRIPT_DIR}/Brewfile"
XCODE_BREWFILE="${SCRIPT_DIR}/Brewfile.xcode"
XCODE_VERSION_FILE="${SCRIPT_DIR}/.xcode-version"
TRUSTED_XCODES_TAP="xcodesorg/made"

ACTION="check"
ACCEPT_ANDROID_LICENSES="false"
CONNECT_AGENT="false"
INSTALL_XCODE="false"
ACCEPT_XCODE_LICENSE="false"
INSTALL_CONTROLLER_TUNNEL="false"
INSTALL_HEALTH_MONITOR="false"

usage() {
  cat <<'EOF'
Usage:
  bootstrap-mac-agent.sh check
  bootstrap-mac-agent.sh install [--install-xcode] [--accept-xcode-license]
    [--accept-android-licenses]
    [--install-controller-tunnel] [--connect-agent] [--install-health-monitor]

Options:
  --install-xcode            按仓库 .xcode-version 交互式安装并选择 Xcode。
  --accept-xcode-license     执行 Xcode 首次启动任务并接受对应许可证。
  --accept-android-licenses  接受服务账号 Android SDK 许可证并安装固定版本 SDK。
  --install-controller-tunnel 安装到 Linux Controller 的常驻 SSH 隧道。
  --connect-agent           工具链通过后调用 install-pr-agent-launchdaemon.sh。
  --install-health-monitor  安装每 5 分钟执行一次的 Jenkins 外部健康监控。

连接 Agent 时通过环境变量传入：
  JENKINS_URL、JENKINS_PR_AGENT_SECRET_FILE（mac-build 节点的 Secret）。
安装 SSH 隧道时还需传入：
  JENKINS_CONTROLLER_SSH_HOST、JENKINS_CONTROLLER_SSH_USER、
  JENKINS_CONTROLLER_SSH_KEY_FILE、JENKINS_CONTROLLER_KNOWN_HOSTS_FILE。
安装健康监控时还需传入：
  DINGTALK_WEBHOOK_FILE、DINGTALK_SECRET_FILE、
  JENKINS_MONITOR_USER_FILE、JENKINS_MONITOR_TOKEN_FILE。
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
    --install-xcode)
      INSTALL_XCODE="true"
      ;;
    --accept-xcode-license)
      ACCEPT_XCODE_LICENSE="true"
      ;;
    --connect-agent)
      CONNECT_AGENT="true"
      ;;
    --install-controller-tunnel)
      INSTALL_CONTROLLER_TUNNEL="true"
      ;;
    --install-health-monitor)
      INSTALL_HEALTH_MONITOR="true"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This bootstrap script only supports macOS." >&2
  exit 1
fi
if [[ "${ACTION}" == "check" && "${ACCEPT_ANDROID_LICENSES}" == "true" ]]; then
  echo "--accept-android-licenses is only valid with install." >&2
  exit 2
fi
if [[ "${ACTION}" == "check" && "${CONNECT_AGENT}" == "true" ]]; then
  echo "--connect-agent is only valid with install." >&2
  exit 2
fi
if [[ "${ACTION}" == "check" && "${INSTALL_CONTROLLER_TUNNEL}" == "true" ]]; then
  echo "--install-controller-tunnel is only valid with install." >&2
  exit 2
fi
if [[ "${ACTION}" == "check" && "${INSTALL_HEALTH_MONITOR}" == "true" ]]; then
  echo "--install-health-monitor is only valid with install." >&2
  exit 2
fi
if [[ "${ACTION}" == "check" && ( "${INSTALL_XCODE}" == "true" || "${ACCEPT_XCODE_LICENSE}" == "true" ) ]]; then
  echo "Xcode install/license options are only valid with install." >&2
  exit 2
fi

require_command() {
  local name="$1"
  command -v "${name}" >/dev/null 2>&1 || {
    echo "Missing required command: ${name}" >&2
    return 1
  }
}

check_xcode() {
  local developer_dir required_version selected_version
  required_version="$(tr -d '[:space:]' < "${XCODE_VERSION_FILE}")"
  developer_dir="$(xcode-select -p 2>/dev/null || true)"
  [[ -n "${developer_dir}" && -d "${developer_dir}" ]] || {
    echo "Xcode is unavailable. Install full Xcode and run xcode-select first." >&2
    return 1
  }
  DEVELOPER_DIR="${developer_dir}" xcodebuild -checkFirstLaunchStatus >/dev/null || {
    echo "Xcode first-launch setup is incomplete. Open Xcode and accept the license/components." >&2
    return 1
  }
  selected_version="$(DEVELOPER_DIR="${developer_dir}" xcodebuild -version | sed -n 's/^Xcode //p')"
  [[ "${selected_version}" == "${required_version}" ]] || {
    echo "Xcode ${required_version} is required, found ${selected_version:-unknown}." >&2
    return 1
  }
}

install_xcode() {
  local required_version
  [[ -s "${XCODE_VERSION_FILE}" ]] || {
    echo "Missing Xcode version file: ${XCODE_VERSION_FILE}" >&2
    exit 1
  }
  required_version="$(tr -d '[:space:]' < "${XCODE_VERSION_FILE}")"
  require_command xcodes
  # xcodes 从 Apple 下载 Xcode，并在终端交互式请求 Apple ID、2FA 和管理员密码。
  if ! xcodes select "${required_version}"; then
    xcodes install "${required_version}"
    xcodes select "${required_version}"
  fi
}

finish_xcode_first_launch() {
  if [[ "${ACCEPT_XCODE_LICENSE}" != "true" ]]; then
    return
  fi
  echo "将执行 Xcode 首次启动任务并接受 ${XCODE_VERSION_FILE} 中版本对应的许可证。"
  sudo xcodebuild -runFirstLaunch
}

resolve_java_home() {
  local candidate prefix
  for candidate in \
    "${JAVA_HOME:-}" \
    "$(/usr/libexec/java_home -v 21 2>/dev/null || true)"; do
    if [[ -n "${candidate}" && -x "${candidate}/bin/java" ]]; then
      printf '%s\n' "${candidate}"
      return
    fi
  done
  prefix="$(brew --prefix openjdk@21 2>/dev/null || true)"
  candidate="${prefix}/libexec/openjdk.jdk/Contents/Home"
  [[ -n "${prefix}" && -x "${candidate}/bin/java" ]] || return 1
  printf '%s\n' "${candidate}"
}

agent_tool_path() {
  local java_home="$1"
  local path="/opt/homebrew/bin:/usr/local/bin:${java_home}/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  if [[ -x "/Applications/ChatGPT.app/Contents/Resources/codex" ]]; then
    path="/Applications/ChatGPT.app/Contents/Resources:${path}"
  fi
  printf '%s\n' "${path}"
}

install_dependencies() {
  require_command brew
  brew bundle --file="${BREWFILE}"
  if [[ "${INSTALL_XCODE}" == "true" ]]; then
    # Homebrew 6 要求非官方 Tap 显式加入信任列表；这里只信任仓库固定的 xcodes 官方 Tap。
    brew tap "${TRUSTED_XCODES_TAP}"
    brew trust --tap "${TRUSTED_XCODES_TAP}"
    brew bundle --file="${XCODE_BREWFILE}"
  fi
}

check_toolchain() {
  local java_home java_version tool_path
  require_command brew
  check_xcode
  java_home="$(resolve_java_home)"
  [[ -x "${java_home}/bin/java" ]] || {
    echo "JDK 21 is unavailable." >&2
    return 1
  }
  java_version="$("${java_home}/bin/java" -version 2>&1 | head -1)"
  [[ "${java_version}" == *'"21.'* ]] || {
    echo "JDK 21 is required, found: ${java_version:-unknown}." >&2
    return 1
  }
  tool_path="$(agent_tool_path "${java_home}")"
  PATH="${tool_path}" require_command python3
  PATH="${tool_path}" require_command git
  PATH="${tool_path}" require_command curl
  PATH="${tool_path}" require_command pod
  [[ -x "$(brew --prefix ruby)/bin/ruby" ]] || {
    echo "Homebrew Ruby is unavailable." >&2
    return 1
  }
  echo "Xcode、JDK 21、CocoaPods、Ruby 和基础命令检查通过。"
}

if [[ "${ACTION}" == "install" ]]; then
  install_dependencies
  if [[ "${INSTALL_XCODE}" == "true" ]]; then
    install_xcode
  fi
  finish_xcode_first_launch
  check_xcode
fi

check_toolchain

if [[ "${INSTALL_CONTROLLER_TUNNEL}" == "true" ]]; then
  bash "${SCRIPT_DIR}/install-controller-tunnel-launchd.sh"
  JENKINS_URL="${JENKINS_URL:-http://127.0.0.1:${JENKINS_CONTROLLER_LOCAL_PORT:-18080}}"
  export JENKINS_URL
fi

if [[ "${CONNECT_AGENT}" == "true" ]]; then
  service_options=(install)
  if [[ "${ACCEPT_ANDROID_LICENSES}" == "true" ]]; then
    service_options+=(--accept-android-licenses)
  fi
  JENKINS_PR_AGENT_SECRET_FILE="${JENKINS_PR_AGENT_SECRET_FILE:-${JENKINS_AGENT_SECRET_FILE:-}}" \
    bash "${SCRIPT_DIR}/install-pr-agent-launchdaemon.sh" "${service_options[@]}"
elif [[ "${ACTION}" == "check" ]]; then
  bash "${SCRIPT_DIR}/install-pr-agent-launchdaemon.sh" check
fi

if [[ "${INSTALL_HEALTH_MONITOR}" == "true" ]]; then
  JENKINS_MONITOR_URL="${JENKINS_MONITOR_URL:-${JENKINS_URL:-http://127.0.0.1:18080}}" \
    bash "${SCRIPT_DIR}/install-health-monitor-launchd.sh"
fi
