#!/usr/bin/env python3
"""发送不含完整控制台日志的 Jenkins 失败/不稳定告警。"""

import argparse
import os
import sys

from pathlib import Path
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delivery.delivery import DeliveryContextError, optional_zentao_work_item
from delivery.dingtalk_notify import DingTalkError, build_failure_markdown, send_failure_notification


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("--build-number", required=True)
    parser.add_argument("--build-url", default="")
    parser.add_argument("--result", choices=("FAILURE", "UNSTABLE"), required=True)
    parser.add_argument("--stage", default="Pipeline")
    parser.add_argument("--platform", default="unknown")
    parser.add_argument("--brand", default="")
    parser.add_argument("--ui-engine", default="")
    parser.add_argument("--git-ref", default="")
    parser.add_argument("--work-item-type", default="")
    parser.add_argument("--work-item-id", default="")
    parser.add_argument("--work-item-url", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        try:
            work_item = optional_zentao_work_item(
                args.work_item_type,
                args.work_item_id,
                args.work_item_url,
            )
        except DeliveryContextError:
            # 告警不能因原始构建参数中的无效工作项而再次失败。
            work_item = None
            print("忽略无效的禅道工作项引用。", file=sys.stderr)
        if args.dry_run:
            print(
                build_failure_markdown(
                    job=args.job,
                    build_number=args.build_number,
                    result=args.result,
                    stage=args.stage,
                    platform=args.platform,
                    brand=args.brand,
                    git_ref=args.git_ref,
                    build_url=args.build_url,
                    work_item=work_item,
                    ui_engine=args.ui_engine,
                )
            )
            return 0
        send_failure_notification(
            job=args.job,
            build_number=args.build_number,
            result=args.result,
            stage=args.stage,
            platform=args.platform,
            brand=args.brand,
            git_ref=args.git_ref,
            build_url=args.build_url,
            webhook=os.environ.get("DINGTALK_WEBHOOK", ""),
            secret=os.environ.get("DINGTALK_SECRET", ""),
            work_item=work_item,
            ui_engine=args.ui_engine,
        )
        print("钉钉 Jenkins 异常通知发送成功。")
        return 0
    except DingTalkError as error:
        print("钉钉 Jenkins 异常通知失败：{}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
