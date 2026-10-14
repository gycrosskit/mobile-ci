#!/usr/bin/env bash
# 固定渲染模板调用；只初始化空的新卷，不认领未知已有内容。
set -euo pipefail
umask 077
nodes_sha="${1:?}"; deployment_sha="${2:?}"; profiles="${3:?}"; profile_agents="${4-}"; profile_proofs="${5:?}"
shift 5
[[ "${nodes_sha}" =~ ^[0-9a-f]{64}$ && "${deployment_sha}" =~ ^[0-9a-f]{64}$ ]] || exit 2
[[ "$(sha256sum /source/nodes.json | cut -d' ' -f1)" == "${nodes_sha}" && "$(sha256sum /source/deployment.json | cut -d' ' -f1)" == "${deployment_sha}" ]] || {
  echo 'Deployment source digest mismatch.' >&2; exit 1;
}
check_dir() {
  local dir="$1"
  [[ -d "${dir}" && ! -L "${dir}" ]] || exit 1
  if [[ "$(stat -c '%u:%g:%a' "${dir}")" != '1000:1000:700' ]]; then
    [[ "$(stat -c '%u' "${dir}")" == 0 && -z "$(find "${dir}" -mindepth 1 -maxdepth 1 -print -quit)" ]] || {
      echo 'Unknown existing volume ownership/mode; refuse takeover.' >&2; exit 1;
    }
  fi
}
check_file() {
  local file="$1" source="$2"
  if [[ -e "${file}" || -L "${file}" ]]; then
    [[ -f "${file}" && ! -L "${file}" && "$(stat -c '%u:%g:%a:%h' "${file}")" == '1000:1000:600:1' ]] || exit 1
    cmp -s "${source}" "${file}" || { echo 'Installed configuration differs; explicit migration required.' >&2; exit 1; }
  fi
}
for dir in "$@"; do
  [[ "${dir}" =~ ^[a-z][a-z0-9-]*$ ]] || exit 2
  check_dir "/init/${dir}"
done
check_dir /memory
for dir in /init/controller-home/ci-deployment /init/controller-home/ci-projects /init/controller-home/init.groovy.d; do
  [[ ! -e "${dir}" && ! -L "${dir}" ]] || check_dir "${dir}"
done
check_file /init/controller-home/ci-deployment/nodes.json /source/nodes.json
check_file /init/controller-home/ci-deployment/deployment.json /source/deployment.json
IFS=',' read -r -a profile_ids <<< "${profiles}"
IFS=',' read -r -a proof_values <<< "${profile_proofs}"
[[ "${#proof_values[@]}" == "${#profile_ids[@]}" ]] || exit 2
for proof in "${proof_values[@]}"; do
  [[ "${proof}" =~ ^([a-z][a-z0-9-]*):([0-9a-f]{64})$ ]] || exit 2
  pid="${BASH_REMATCH[1]}"; expected="${BASH_REMATCH[2]}"
  [[ ",${profiles}," == *",${pid},"* && "$(sha256sum "/source/profiles/${pid}.json" | cut -d' ' -f1)" == "${expected}" ]] || {
    echo 'Trusted profile source digest mismatch.' >&2; exit 1;
  }
done
for pid in "${profile_ids[@]}"; do
  [[ "${pid}" =~ ^[a-z][a-z0-9-]*$ && -f "/source/profiles/${pid}.json" && ! -L "/source/profiles/${pid}.json" ]] || exit 2
  check_file "/init/controller-home/ci-projects/${pid}.json" "/source/profiles/${pid}.json"
done
IFS=',' read -r -a profile_agent_ids <<< "${profile_agents}"
for aid in "${profile_agent_ids[@]}"; do
  [[ "${aid}" =~ ^[a-z][a-z0-9-]*$ ]] || exit 2
  for dir in "/profiles/${aid}" "/profiles/${aid}/projects"; do
    if [[ -e "${dir}" || -L "${dir}" ]]; then
      [[ -d "${dir}" && ! -L "${dir}" ]] || exit 1
      if [[ "$(stat -c '%u:%g:%a' "${dir}")" != '0:1000:750' ]]; then
        [[ "$(stat -c '%u' "${dir}")" == 0 && -z "$(find "${dir}" -mindepth 1 -maxdepth 1 -print -quit)" ]] || exit 1
      fi
    fi
  done
  for pid in "${profile_ids[@]}"; do
    file="/profiles/${aid}/projects/${pid}.json"
    if [[ -e "${file}" || -L "${file}" ]]; then
      [[ -f "${file}" && ! -L "${file}" && "$(stat -c '%u:%g:%a:%h' "${file}")" == '0:1000:440:1' ]] || exit 1
      cmp -s "/source/profiles/${pid}.json" "${file}" || { echo 'Installed agent profile differs; explicit migration required.' >&2; exit 1; }
    fi
  done
done

if [[ -e /memory/build.lock || -L /memory/build.lock ]]; then
  [[ -f /memory/build.lock && ! -L /memory/build.lock && "$(stat -c '%u:%g:%a:%h' /memory/build.lock)" == '1000:1000:600:1' ]] || exit 1
fi
# 先完成所有已有状态与内容检查，才初始化本次明确声明的空卷。
for dir in "$@"; do install -d -o 1000 -g 1000 -m 700 "/init/${dir}"; done
install -d -o 1000 -g 1000 -m 700 /memory /init/controller-home/ci-deployment /init/controller-home/ci-projects /init/controller-home/init.groovy.d
install_new() {
  local source="$1" target="$2" temp
  [[ ! -e "${target}" ]] || return 0
  temp="$(mktemp "${target}.new.XXXXXX")"
  install -o 1000 -g 1000 -m 600 "${source}" "${temp}"
  mv -n "${temp}" "${target}"
  rm -f "${temp}"
}
install_new /source/nodes.json /init/controller-home/ci-deployment/nodes.json
install_new /source/deployment.json /init/controller-home/ci-deployment/deployment.json
for pid in "${profile_ids[@]}"; do install_new "/source/profiles/${pid}.json" "/init/controller-home/ci-projects/${pid}.json"; done
if [[ ! -e /memory/build.lock ]]; then
  (set -C; : > /memory/build.lock)
  chown 1000:1000 /memory/build.lock
  chmod 600 /memory/build.lock
fi

for aid in "${profile_agent_ids[@]}"; do
  install -d -o 0 -g 1000 -m 750 "/profiles/${aid}" "/profiles/${aid}/projects"
  for pid in "${profile_ids[@]}"; do
    target="/profiles/${aid}/projects/${pid}.json"
    if [[ ! -e "${target}" ]]; then
      temp="$(mktemp "${target}.new.XXXXXX")"
      install -o 0 -g 1000 -m 440 "/source/profiles/${pid}.json" "${temp}"
      mv -n "${temp}" "${target}"
      rm -f "${temp}"
    fi
  done
done
