#!/usr/bin/env python3
"""单模块示例对任何已追踪变更执行同一组任务，不声明伪造的 SDK 验证结果。"""
import argparse
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
TASKS = [":mobile:lintDebug", ":mobile:testDebugUnitTest"]
parser = argparse.ArgumentParser()
parser.add_argument("--base-ref", required=True)
parser.add_argument("--execute", action="store_true")
args = parser.parse_args()
subprocess.run(["git", "rev-parse", "--verify", args.base_ref + "^{commit}"], cwd=ROOT, check=True, capture_output=True)
changed = subprocess.check_output(["git", "diff", "--name-only", args.base_ref, "HEAD"], cwd=ROOT, text=True).strip()
if changed:
    if args.execute:
        subprocess.run(["bash", "gradlew", "--no-daemon", *TASKS], cwd=ROOT, check=True)
    else:
        print("\n".join(TASKS))
