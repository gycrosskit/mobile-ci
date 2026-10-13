#!/usr/bin/env python3
"""记录测试包的命令耗时和交付文件大小，不记录命令参数或环境变量。"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def append_record(output: Path, record: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def run_command(output: Path, label: str, command: list[str]) -> int:
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("缺少待计时命令")
    started_at = utc_now()
    start = time.monotonic()
    print(f"CI_TIMING_START label={label} at={started_at}", flush=True)
    try:
        result = subprocess.run(command, check=False)
        exit_code = result.returncode
    except OSError:
        exit_code = 127
    finished_at = utc_now()
    duration_ms = round((time.monotonic() - start) * 1000)
    append_record(output, {
        "type": "step",
        "label": label,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
    })
    print(f"CI_TIMING_END label={label} duration_ms={duration_ms} exit_code={exit_code}", flush=True)
    return exit_code


def record_size(output: Path, label: str, path: Path) -> int:
    if not path.is_file():
        print(f"交付文件不存在：{label}", file=sys.stderr)
        return 1
    size_bytes = path.stat().st_size
    append_record(output, {"type": "artifact", "label": label, "size_bytes": size_bytes})
    print(f"CI_ARTIFACT_SIZE label={label} bytes={size_bytes}", flush=True)
    return 0


def path_from_context(context: Path, key: str) -> Path:
    data = json.loads(context.read_text(encoding="utf-8"))
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"交付上下文缺少 {key}")
    return Path(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--label", required=True)
    run.add_argument("command", nargs=argparse.REMAINDER)
    size = subparsers.add_parser("size")
    size.add_argument("--output", type=Path, required=True)
    size.add_argument("--label", required=True)
    source = size.add_mutually_exclusive_group(required=True)
    source.add_argument("--path", type=Path)
    source.add_argument("--context", type=Path)
    size.add_argument("--key")
    args = parser.parse_args()
    if args.action == "run":
        return run_command(args.output, args.label, args.command)
    if args.context and not args.key:
        parser.error("--context 需要 --key")
    if args.key and not args.context:
        parser.error("--key 需要 --context")
    path = path_from_context(args.context, args.key) if args.context else args.path
    return record_size(args.output, args.label, path)


if __name__ == "__main__":
    sys.exit(main())
