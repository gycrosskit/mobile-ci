#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CI_TOOL_ROOT="$(cd "${CI_TOOL_ROOT:-${SCRIPT_DIR}/..}" && pwd)"
REPO_ROOT="$(cd "${APP_SOURCE_ROOT:-${PWD}}" && pwd)"
[[ -f "${REPO_ROOT}/gradlew" ]] || {
  echo "APP_SOURCE_ROOT must point to the application checkout (gradlew required)." >&2
  exit 2
}
export CI_TOOL_ROOT APP_SOURCE_ROOT="${REPO_ROOT}"
export PYTHONPATH="${CI_TOOL_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

usage() {
  cat <<'EOF'
Usage:
  android_ci.sh describe <brand> <test|release> [cmp|kuikly]
  android_ci.sh quality-gate <brand> <test|release> [cmp|kuikly]
  android_ci.sh verified-build <brand> <test|release> [cmp|kuikly]
  android_ci.sh mapping-dir <brand> <test|release> [cmp|kuikly]
EOF
}

resolve_variant() {
  local brand="$1"
  local pipeline="$2"
  [[ "${brand}" =~ ^[a-z][a-z0-9_]*$ ]] || {
    echo "Invalid Android brand: ${brand}" >&2
    exit 2
  }
  local contract line
  contract="$(python3 "${CI_TOOL_ROOT}/project.py" android "${brand}" "${pipeline}" "${CI_UI_ENGINE}")" || exit 2
  local values=()
  while IFS= read -r line; do values+=("${line}"); done <<< "${contract}"
  [[ ${#values[@]} -eq 5 ]] || exit 2
  APP_MODULE="${values[0]}"; BUILD_TYPE="${values[1]}"; VARIANT="${values[2]}"
  MAPPING_DIR="${values[3]}"; ADAPTER="${values[4]}"
  CMP_BRAND="${brand}"
  local gradle_module="${APP_MODULE//\//:}"
  LINT_TASK=":${gradle_module}:lint${VARIANT}"
  ASSEMBLE_TASK=":${gradle_module}:assemble${VARIANT}"
}

run_gradle() {
  (
    cd "${REPO_ROOT}"
    local project_args=()
    [[ "${ADAPTER}" != "sxmqlive" ]] || project_args+=("-PcmpBrand=${CMP_BRAND}")
    bash gradlew \
      --no-daemon \
      --max-workers=2 \
      --parallel \
      --profile \
      '-Dorg.gradle.jvmargs=-Xmx3072m -Dfile.encoding=UTF-8' \
      -Pkotlin.compiler.execution.strategy=in-process \
      ${project_args[@]+"${project_args[@]}"} \
      "$@"
  )
}

command="${1:-}"
[[ $# -eq 3 || $# -eq 4 ]] || { usage; exit 2; }
CI_UI_ENGINE="${4-${CI_UI_ENGINE-cmp}}"
export CI_UI_ENGINE
resolve_variant "$2" "$3"

case "${command}" in
  describe)
    printf 'Lint task: %s\n' "${LINT_TASK}"
    printf 'Build variant: %s\n' "${ASSEMBLE_TASK}"
    ;;
  quality-gate)
    (
      cd "${CI_TOOL_ROOT}"
      python3 -m unittest -b \
        tests.delivery.test_ci_execution_contracts \
        tests.delivery.test_release
      cd "${REPO_ROOT}"
      if [[ "${ADAPTER}" != "sxmqlive" ]]; then
        run_gradle "${LINT_TASK}"
      elif [[ -f "ci/tests/test_manifest_contract.py" ]]; then
        PYTHONPATH="${REPO_ROOT}:${PYTHONPATH}" python3 -m unittest -b \
          ci.tests.test_manifest_contract ci.tests.test_r8_configuration
      else
        # 历史应用分支仍由其原有契约检查负责，缺入口时严格失败。
        PYTHONPATH="${REPO_ROOT}:${PYTHONPATH}" python3 -m unittest -b \
          tests.tools.test_manifest_contract tests.tools.test_r8_configuration
      fi
    )
    ;;
  verified-build)
    run_gradle "${LINT_TASK}" "${ASSEMBLE_TASK}"
    ;;
  mapping-dir)
    printf '%s\n' "${MAPPING_DIR}"
    ;;
  *)
    usage
    exit 2
    ;;
esac
