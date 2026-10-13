#!/usr/bin/env python3
"""解析 iOS 测试设备管理任务需要处理的品牌和 Developer Team。"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Mapping, Sequence

from project import SOURCE_ROOT

from delivery.ios_release import IosReleaseError, load_ios_brand_config


REPO_ROOT = SOURCE_ROOT
DEFAULT_CONFIG = REPO_ROOT / "ci" / "brands.json"
BRAND_NAME = re.compile(r"[a-z][a-z0-9_]*")


class IosDeviceError(RuntimeError):
    """设备管理参数或品牌签名配置无效。"""


def _load_config(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IosDeviceError("无法读取品牌配置：{}".format(path)) from error
    if not isinstance(value, dict):
        raise IosDeviceError("ci/brands.json 根节点必须是对象")
    return value


def resolve_device_brands(config_path: Path, requested: str) -> Sequence[str]:
    """返回已启用 iOS 测试签名的品牌；all 按配置顺序选择全部品牌。"""
    root = _load_config(config_path)
    available = []
    for brand, value in root.items():
        if brand.startswith("_") or not isinstance(value, dict):
            continue
        ios = value.get("ios")
        signing = ios.get("signing") if isinstance(ios, dict) else None
        test_signing = signing.get("test") if isinstance(signing, dict) else None
        if isinstance(test_signing, dict) and test_signing.get("enabled") is True:
            available.append(brand)
    if not available:
        raise IosDeviceError("没有启用 iOS 测试签名的品牌")

    value = requested.strip().lower()
    if value == "all":
        selected = available
    else:
        selected = []
        for item in requested.split(","):
            brand = item.strip().lower()
            if not BRAND_NAME.fullmatch(brand):
                raise IosDeviceError("无效品牌参数：{}".format(item.strip() or "<empty>"))
            if brand not in selected:
                selected.append(brand)
        if not selected:
            raise IosDeviceError("TARGET_BRANDS 不能为空")

    unavailable = [brand for brand in selected if brand not in available]
    if unavailable:
        raise IosDeviceError(
            "品牌未启用 iOS 测试签名：{}".format(", ".join(unavailable))
        )
    for brand in selected:
        try:
            load_ios_brand_config(config_path, brand, "test")
        except IosReleaseError as error:
            raise IosDeviceError(str(error)) from error
    return selected


def print_plan(args: argparse.Namespace) -> int:
    for brand in resolve_device_brands(args.config, args.brands):
        print(brand)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    plan = subparsers.add_parser("plan", help="输出设备管理任务需要处理的 iOS 品牌")
    plan.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    plan.add_argument(
        "--brands",
        required=True,
        help="all 或英文逗号分隔的品牌 ID",
    )
    plan.set_defaults(action=print_plan)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "action"):
        parser.print_help(sys.stderr)
        return 2
    try:
        return args.action(args)
    except IosDeviceError as error:
        print("iOS 设备管理失败：{}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
