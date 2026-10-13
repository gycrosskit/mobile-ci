#!/usr/bin/env python3
"""从 Jenkins 外部检查 Controller 与 Agent，并在状态变化时发送钉钉告警。"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delivery.dingtalk_notify import DingTalkError, build_health_markdown, send_health_notification
from delivery.delivery import delivery_urlopen as urlopen


def _request_json(
    url: str,
    timeout: float,
    username: str = "",
    token: str = "",
) -> Dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "sxmqlive-jenkins-monitor/1"}
    if username and token:
        encoded = base64.b64encode("{}:{}".format(username, token).encode("utf-8")).decode("ascii")
        headers["Authorization"] = "Basic {}".format(encoded)
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def inspect_jenkins(
    base_url: str,
    timeout: float,
    username: str = "",
    token: str = "",
) -> Tuple[str, str]:
    root = base_url.rstrip("/")
    login_request = Request(root + "/login", headers={"User-Agent": "sxmqlive-jenkins-monitor/1"})
    try:
        with urlopen(login_request, timeout=timeout) as response:
            status_code = response.status
    except HTTPError as error:
        status_code = error.code
    except (URLError, TimeoutError, OSError) as error:
        return "down", "Controller 不可达：{}".format(type(error).__name__)
    if status_code >= 500:
        return "down", "Controller 返回 HTTP {}".format(status_code)
    if not 200 <= status_code < 300:
        return "degraded", "Controller 检查返回 HTTP {}".format(status_code)
    if not (username and token):
        return "healthy", "Controller 可访问；未配置监控凭据，跳过 Agent 明细"

    query = urlencode({"tree": "computer[displayName,offline,temporarilyOffline]"})
    try:
        payload = _request_json(root + "/computer/api/json?" + query, timeout, username, token)
    except HTTPError as error:
        return "degraded", "Controller 可访问，但 Agent API 返回 HTTP {}".format(error.code)
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
        return "degraded", "Controller 可访问，但 Agent 明细读取失败：{}".format(type(error).__name__)
    computers = payload.get("computer") if isinstance(payload, dict) else None
    if not isinstance(computers, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("offline"), bool)
        or not isinstance(item.get("temporarilyOffline"), bool)
        for item in computers
    ):
        return "degraded", "Controller 可访问，但 Agent 明细结构无效"
    offline = [
        str(item.get("displayName") or "unknown")
        for item in computers
        if item["offline"] and not item["temporarilyOffline"]
    ]
    if offline:
        return "degraded", "离线 Agent：{}".format("、".join(offline[:10]))
    return "healthy", "Controller 与 {} 个 Agent 状态正常".format(len(computers))


def load_state(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(str(temporary), 0o600)
    temporary.replace(path)


def should_notify(previous: Dict[str, Any], status: str, now: int, repeat_seconds: int) -> bool:
    old_status = str(previous.get("status") or "")
    if old_status != status:
        return bool(old_status) or status != "healthy"
    if status == "healthy":
        return False
    last_notified = int(previous.get("lastNotifiedAt") or 0)
    return now - last_notified >= repeat_seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("JENKINS_MONITOR_URL", "http://127.0.0.1:18080"))
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--repeat-minutes", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    now = int(time.time())
    previous = load_state(args.state_file)
    status, summary = inspect_jenkins(
        args.url,
        args.timeout,
        os.environ.get("JENKINS_MONITOR_USER", ""),
        os.environ.get("JENKINS_MONITOR_TOKEN", ""),
    )
    notify = should_notify(previous, status, now, max(1, args.repeat_minutes) * 60)
    old_status = str(previous.get("status") or "")
    last_notified = int(previous.get("lastNotifiedAt") or 0)
    try:
        if notify:
            if args.dry_run:
                print(build_health_markdown(status, summary, args.url, old_status))
            else:
                send_health_notification(
                    status=status,
                    summary=summary,
                    jenkins_url=args.url,
                    previous_status=old_status,
                    webhook=os.environ.get("DINGTALK_WEBHOOK", ""),
                    secret=os.environ.get("DINGTALK_SECRET", ""),
                )
                last_notified = now
    except DingTalkError as error:
        print("Jenkins 外部健康告警发送失败：{}".format(error), file=sys.stderr)
        return 2
    # 预演不能消费故障/恢复转换，否则下一次真实检查会遗漏通知。
    if not args.dry_run:
        save_state(
            args.state_file,
            {"status": status, "summary": summary, "checkedAt": now, "lastNotifiedAt": last_notified},
        )
    print("Jenkins 状态：{}；{}".format(status, summary))
    return 1 if status == "down" else 0


if __name__ == "__main__":
    sys.exit(main())
