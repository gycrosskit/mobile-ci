#!/usr/bin/env python3
"""定义 Android/iOS 发布脚本共享的中立交付模型。"""

from contextlib import contextmanager, ExitStack
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tempfile
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, NamedTuple, Optional, Type
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

VERSION_PATTERN = r"[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9]+)*"
BRAND_PATTERN = r"[a-z][a-z0-9_]*"
VALID_BRAND_NAME = re.compile(r"^{}$".format(BRAND_PATTERN))
UI_ENGINES = ("cmp", "kuikly", "native")
RELEASE_BRANCH = re.compile(
    r"^release/(?P<brand>{})-(?P<version>{})$".format(
        BRAND_PATTERN,
        VERSION_PATTERN,
    )
)
DEV_BRANCH = re.compile(r"^dev/(?P<brand>{})$".format(BRAND_PATTERN))
FEATURE_BRANCH = re.compile(
    (
        r"^feature/(?P<brand>{})/"
        r"(?P<summary>[a-z0-9][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)*)$"
    ).format(BRAND_PATTERN)
)
DELIVERY_RETRY_DELAYS = (1.0, 3.0, 5.0)
STATE_ARCHIVE_LIMIT = 64 * 1024 * 1024
STATE_ARCHIVE_FILES = 10000
DELIVERY_LOCK_MARKER = b"delivery-lock-v1\n"
ZENTAO_WORK_ITEM_TYPES = ("story", "bug", "task", "requirement", "epic")
WORK_ITEM_TYPE_LABELS = {
    "story": "需求",
    "bug": "Bug",
    "task": "任务",
    "requirement": "用户需求",
    "epic": "业务需求",
}
WORK_ITEM_ID = re.compile(r"^[1-9][0-9]*$")


def normalize_branch(branch: str) -> str:
    value = branch.strip()
    for prefix in ("refs/heads/", "origin/"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


class DeliveryContextError(ValueError):
    """发布上下文缺少跨平台交付所需的稳定字段。"""


def artifact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _NoDeliveryRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def delivery_urlopen(request, timeout):
    # 重定向后的 GET 响应不能证明原 POST 被拒绝，否则会错误重发已接受的写入。
    return build_opener(_NoDeliveryRedirects()).open(request, timeout=timeout)


def delivery_state_root() -> Path:
    """预检与真实交付共用路径、所有者和权限约束，避免配置漂移。"""
    configured = os.environ.get("DELIVERY_STATE_ROOT", "")
    root = Path(configured)
    if not configured or not root.is_absolute():
        raise DeliveryContextError("必须设置工作区外的绝对 DELIVERY_STATE_ROOT")
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise DeliveryContextError("DELIVERY_STATE_ROOT 不允许符号链接")
    root = root.resolve()
    for workspace in (str(Path.cwd()), os.environ.get("WORKSPACE"),
                      os.environ.get("APP_SOURCE_ROOT"), os.environ.get("CI_TOOL_ROOT")):
        if workspace and (Path(workspace).resolve() == root or Path(workspace).resolve() in root.parents):
            raise DeliveryContextError("DELIVERY_STATE_ROOT 必须位于源码和工作区外")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = root.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise DeliveryContextError("交付状态目录必须由当前用户所有且权限为 0700")
    return root


def inspect_state_archive(path: Path, expected_uid=None, private=False, outside=None) -> dict:
    """备份与恢复共用回执归档边界；只返回完整性和覆盖元数据。"""
    path = Path(path)
    if private:
        if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
            raise DeliveryContextError("回执输入必须是无符号链接的绝对路径")
        if outside and (Path(outside).resolve() == path.resolve() or Path(outside).resolve() in path.resolve().parents):
            raise DeliveryContextError("回执输入必须位于 Jenkins Home 外")
        parent = path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise DeliveryContextError("回执输入父目录必须为当前用户所有的 0700")
    # FIFO 必须先无阻塞打开，再由 fstat 拒绝，不能等写入方才进入类型检查。
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > STATE_ARCHIVE_LIMIT:
            raise DeliveryContextError("回执归档必须为单链接有界常规文件")
        if private and (metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise DeliveryContextError("回执输入必须为当前用户所有的 0600")
        compressed = source.read(STATE_ARCHIVE_LIMIT + 1)
        after = os.fstat(source.fileno())
        if len(compressed) != metadata.st_size or (metadata.st_size, metadata.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise DeliveryContextError("读取期间回执归档改变")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(STATE_ARCHIVE_LIMIT + STATE_ARCHIVE_FILES * 2048 + 1)
        if len(raw) > STATE_ARCHIVE_LIMIT + STATE_ARCHIVE_FILES * 2048:
            raise DeliveryContextError("回执归档解压后过大")
        names = set()
        uid, files, size, receipts = None, 0, 0, 0
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for member in archive:
                name = member.name[2:] if member.name.startswith("./") else member.name
                if member.isdir():
                    name = name.rstrip("/")
                if name in names:
                    raise DeliveryContextError("回执归档存在重复路径")
                names.add(name)
                if member.isdir() and name == "delivery-state":
                    if stat.S_IMODE(member.mode) != 0o700:
                        raise DeliveryContextError("回执归档根目录必须为 0700")
                    uid = member.uid
                elif member.isfile() and re.fullmatch(r"delivery-state/[0-9a-f]{64}\.(json|lock)", name):
                    if stat.S_IMODE(member.mode) != 0o600:
                        raise DeliveryContextError("回执归档文件必须为 0600")
                    files += 1
                    size += member.size
                    if files > STATE_ARCHIVE_FILES or size > STATE_ARCHIVE_LIMIT or member.size > 1024 * 1024:
                        raise DeliveryContextError("回执归档文件数或大小超限")
                    if name.endswith(".json"):
                        with archive.extractfile(member) as content:
                            value = json.load(content)
                        if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1
                                or not isinstance(value.get("identity"), dict) or not ({"phase", "status"} & value.keys())):
                            raise DeliveryContextError("回执归档 JSON 身份或状态无效")
                        receipts += 1
                else:
                    raise DeliveryContextError("回执归档存在越界、链接或非回执节点")
                if expected_uid is not None and member.uid != expected_uid:
                    raise DeliveryContextError("回执归档所有者不符合源节点")
            if uid is None or uid <= 0 or any(member.uid != uid for member in archive.getmembers()):
                raise DeliveryContextError("回执归档必须包含同一非 root 用户所有的根目录和文件")
    except (OSError, EOFError, ValueError, UnicodeError, tarfile.TarError) as error:
        if isinstance(error, DeliveryContextError):
            raise
        raise DeliveryContextError("回执归档损坏；停止恢复") from error
    return {"uid": uid, "files": files, "bytes": size, "receipts": receipts,
            "sha256": hashlib.sha256(compressed).hexdigest()}


def validate_state_coverage(value: dict) -> dict:
    if (not isinstance(value, dict) or set(value) != {"version", "linux", "mac", "macSourceUid"}
            or type(value.get("version")) is not int or value["version"] != 1
            or value["linux"] not in (None, "linux-delivery-state.tar.gz")
            or value["mac"] not in (None, "mac-delivery-state.tar.gz")
            or (value["mac"] is None and value["macSourceUid"] is not None)
            or (value["mac"] is not None and (type(value["macSourceUid"]) is not int or value["macSourceUid"] <= 0))):
        raise DeliveryContextError("回执覆盖声明无效")
    return value


def pgyer_build_key_matches(upload_key, published_key, platform) -> bool:
    if not all(isinstance(value, str) and value for value in (upload_key, published_key, platform)):
        return False
    suffix = {"android": ".apk", "ios": ".ipa", "harmonyos": ".hap"}.get(platform)
    if suffix is None:
        return False
    # COS 存储 key 带平台扩展名；发布版本 ID 只允许精确去掉这个末尾后缀。
    return (published_key == upload_key or
            upload_key.endswith(suffix) and len(upload_key) > len(suffix)
            and published_key == upload_key[:-len(suffix)])


def read_delivery_record(source_path):
    descriptor = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1 or metadata.st_size > 1024 * 1024):
            raise DeliveryContextError("交付回执类型、权限或所有者无效")
        try:
            value = json.load(source)
        except (ValueError, UnicodeError) as error:
            raise DeliveryContextError("交付回执损坏；停止执行并保留状态") from error
    if (not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1
            or not isinstance(value.get("identity"), dict)):
        raise DeliveryContextError("交付回执身份或版本无效；停止执行")
    if "phase" not in value and "status" not in value:
        raise DeliveryContextError("交付回执缺少操作状态；停止执行并保留状态")
    previous_ns = value.get("namespace")
    if previous_ns is not None and (not isinstance(previous_ns, dict)
            or set(previous_ns) != {"project_id", "provider_account_id"}
            or any(not isinstance(v, str) for v in previous_ns.values())):
        raise DeliveryContextError("交付回执 namespace 损坏；停止执行")
    return value


def delivery_receipt(scope, provider="pgyer"):
    """只读已持久化结果；不创建锁、不请求供应商、不认领未知旧台账。"""
    namespace, legacy = delivery_namespace(provider)
    candidates = []
    for path in delivery_state_root().glob("*.json"):
        value = read_delivery_record(path)
        identity = value["identity"]
        if any(identity.get(key) != expected for key, expected in scope.items()):
            continue
        previous = value.get("namespace")
        if previous != namespace and not (previous is None and legacy):
            continue
        key_input = scope if legacy else {**scope, **namespace}
        keys = [hashlib.sha256(json.dumps(key_input, sort_keys=True, ensure_ascii=False).encode()).hexdigest()]
        if legacy:
            keys.append(hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        if path.stem in keys:
            candidates.append(value)
    if len(candidates) != 1:
        raise DeliveryContextError("未找到唯一可信分发回执；停止通知")
    return candidates[0]


@contextmanager
def delivery_operation(identity: Mapping[str, Any], scope: Optional[Mapping[str, Any]] = None,
                       *, namespace=None, legacy_compatible=False):
    """锁覆盖请求和回执；可信 namespace 隔离项目，旧台账不能被新锁绕过。"""
    root = delivery_state_root()
    identity = dict(identity)
    if scope is not None and any(identity.get(field) != value for field, value in scope.items()):
        raise DeliveryContextError("交付锁范围必须与完整身份一致")
    if namespace is not None and (not isinstance(namespace, dict)
            or set(namespace) != {"project_id", "provider_account_id"}
            or any(not isinstance(v, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", v) for v in namespace.values())):
        raise DeliveryContextError("交付项目与供应商身份无效")

    def key_for(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    with ExitStack() as locks:
        guard_queued = False
        if namespace is not None:
            # 同项目同操作跨账号/凭据轮换共用短边界，跨项目各自持锁；
            # 必须先持锁再找旧回执，不能在旧 writer 首次落盘前抢先发请求。
            guard_scope = {field: value for field, value in (scope or identity).items() if field != "channel_sha256"}
            guard_key = key_for({**guard_scope, "project_id":namespace["project_id"], "account_guard":True})
            descriptor = os.open(root / (guard_key + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            guard = locks.enter_context(os.fdopen(descriptor, "r+"))
            info = os.fstat(guard.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                raise DeliveryContextError("交付账号锁类型、权限或所有者无效")
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                guard_queued = True
                fcntl.flock(guard, fcntl.LOCK_EX)
            guard.seek(0); guard.truncate(); guard.write(DELIVERY_LOCK_MARKER.decode("ascii")); guard.flush()
            for previous in root.glob("*.lock"):
                if not re.fullmatch(r"[0-9a-f]{64}\.lock", previous.name):
                    continue
                fd = os.open(previous, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as candidate:
                    metadata = os.fstat(candidate.fileno())
                    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                            or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1 or metadata.st_size > 256):
                        raise DeliveryContextError("旧交付锁类型、权限或所有者无效")
                    if candidate.read(257) == DELIVERY_LOCK_MARKER:
                        continue
                    receipt_path = previous.with_suffix(".json")
                    if receipt_path.exists() or receipt_path.is_symlink():
                        continue
                    try:
                        fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as error:
                        raise DeliveryContextError("旧版交付锁仍活跃且归属未知；必须停止旧发布器，禁止重发") from error
        stable_channel = namespace is not None and identity.get("operation") == "release-notify"
        if stable_channel and not legacy_compatible:
            # Bot 凭据可轮换；受信 logical account 才是收件通道身份。
            identity.pop("channel_sha256", None)
            scope = {field: value for field, value in scope.items() if field != "channel_sha256"}
        base_scope = dict(scope if scope is not None else identity)
        legacy_key = key_for(base_scope)
        key = legacy_key if namespace is None or legacy_compatible else key_for({**base_scope, **namespace})
        path = root / (key + ".json")
        if stable_channel and legacy_compatible and not (path.exists() or path.is_symlink()):
            matches = []
            for previous in root.glob("*.json"):
                value = read_delivery_record(previous)
                old = value["identity"]
                if (value.get("namespace") not in (None, namespace)
                        or {k: v for k, v in old.items() if k != "channel_sha256"} != {k: v for k, v in identity.items() if k != "channel_sha256"}):
                    continue
                old_scope = {k: old.get(k) for k in base_scope}
                if previous.stem in (key_for(old_scope), key_for(old)):
                    matches.append((previous, old, old_scope))
            if len(matches) > 1:
                raise DeliveryContextError("旧通知回执通道不唯一；必须核对，禁止重新发送")
            if matches:
                path, identity, base_scope = matches[0]
                scope, key, legacy_key = base_scope, path.stem, key_for(base_scope)
        # 默认项目保持旧 key 和 identity；metadata 不会令回滚版本误认为另一个产物。
        if namespace is not None and legacy_compatible and scope is not None and not (path.exists() or path.is_symlink()):
            full_key = key_for(identity)
            if (root / (full_key + ".json")).exists() or (root / (full_key + ".json")).is_symlink():
                key, path = full_key, root / (full_key + ".json")

        for old_key in sorted({legacy_key, key_for(identity)}):
            legacy_lock, legacy_path = root / (old_key + ".lock"), root / (old_key + ".json")
            if namespace is not None and key != old_key and (legacy_lock.exists() or legacy_lock.is_symlink()):
                previous_ns = read_delivery_record(legacy_path).get("namespace") if legacy_path.exists() or legacy_path.is_symlink() else None
                # 旧 writer 可能持锁但尚未落盘；不能靠新 namespace 抢先发请求。
                if previous_ns is None or previous_ns.get("project_id") == namespace["project_id"]:
                    fd = os.open(legacy_lock, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
                    old_lock = locks.enter_context(os.fdopen(fd, "r+"))
                    info = os.fstat(old_lock.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                        raise DeliveryContextError("旧交付锁类型、权限或所有者无效")
                    fcntl.flock(old_lock, fcntl.LOCK_EX)
        descriptor = os.open(root / (key + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        lock = locks.enter_context(os.fdopen(descriptor, "r+"))
        metadata = os.fstat(lock.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
            raise DeliveryContextError("交付锁类型、权限或所有者无效")
        queued = guard_queued
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            queued = True
            fcntl.flock(lock, fcntl.LOCK_EX)
        if namespace is not None:
            lock.seek(0); lock.truncate(); lock.write(DELIVERY_LOCK_MARKER.decode("ascii")); lock.flush()
        record = {"version": 1, "identity": identity}
        if scope is not None or namespace is not None:
            for previous in root.glob("*.json"):
                if previous == path:
                    continue
                value = read_delivery_record(previous)
                old_identity, previous_ns = value["identity"], value.get("namespace")
                same_operation = all(field not in old_identity or old_identity[field] == expected
                                     for field, expected in base_scope.items() if field != "channel_sha256")
                unfinished = value.get("phase") not in ("complete", "completed", "rejected") and value.get("status") not in ("sent", "rejected")
                if namespace is not None and same_operation and unfinished:
                    if previous_ns is None or previous_ns.get("project_id") == namespace["project_id"] and (
                            previous_ns != namespace or old_identity.get("channel_sha256") != identity.get("channel_sha256")):
                        raise DeliveryContextError("旧供应商账号结果未知或旧版交付回执未确认；禁止通过新账号重发")
                matches = all(field not in old_identity or old_identity[field] == expected for field, expected in base_scope.items())
                if not matches:
                    continue
                if namespace is not None and previous_ns is not None:
                    if previous_ns["project_id"] != namespace["project_id"]:
                        continue
                    if previous_ns != namespace:
                        if value.get("phase") not in ("complete", "completed", "rejected") and value.get("status") not in ("sent", "rejected"):
                            raise DeliveryContextError("旧供应商账号结果未知；必须先核对，禁止通过新账号重发")
                        continue
                if previous.stem in (key_for(old_identity), legacy_key):
                    raise DeliveryContextError("发现旧版交付回执；必须先核对并迁移，禁止以新锁范围重发")
        if path.exists() or path.is_symlink():
            record = read_delivery_record(path)
            previous_ns = record.get("namespace")
            if namespace is not None and ((previous_ns is None and not legacy_compatible)
                                          or previous_ns is not None and previous_ns != namespace):
                raise DeliveryContextError("旧交付台账归属不明或账号不匹配；必须先迁移")
            if scope is not None and any(record["identity"].get(field) != value for field, value in scope.items()):
                raise DeliveryContextError("交付回执锁范围不一致；停止执行")
            if record["identity"] != identity:
                receipt = record.get("receipt")
                if (scope is not None and not queued and record.get("phase") == "complete" and isinstance(receipt, dict)
                        and receipt.get("build_key") == record.get("build_key")
                        and ("upload_key" not in record or pgyer_build_key_matches(
                            record["upload_key"], record["build_key"], record["identity"].get("platform")))
                        and all(isinstance(receipt.get(field), str) and receipt[field] for field in
                                ("name", "version", "version_code", "package_name", "build_key", "download_url"))):
                    record = {"version": 1, "identity": identity}
                else:
                    raise DeliveryContextError("交付结果尚未确认或材料身份不一致；禁止重新上传或发送")

        def save(**fields):
            if {"version", "identity", "namespace"} & fields.keys():
                raise DeliveryContextError("不能修改交付回执身份或版本")
            updated = {**record, **fields}
            if namespace is not None:
                updated["namespace"] = dict(namespace)
            write_json_object(path, updated)
            record.update(updated)
        yield record, save


def validate_ui_engine(value: str) -> str:
    from project import selected_project
    from project_config import ProjectConfigError
    try:
        allowed = set()
        for config in selected_project()["platforms"].values():
            allowed.update(config.get("engines", ["cmp", "kuikly"] if config["adapter"] == "sxmqlive" else ["native"]))
    except (ProjectConfigError, OSError) as error:
        raise DeliveryContextError("可信项目配置无效") from error
    if value not in allowed:
        raise DeliveryContextError("UI 引擎不属于可信项目：{}".format(value))
    return value


def android_app_module(ui_engine: str) -> str:
    from project import android_contract
    return android_contract("placeholder", "test", validate_ui_engine(ui_engine))["module"]


def delivery_namespace(provider):
    from project import provider_namespace
    from project_config import ProjectConfigError
    try:
        return provider_namespace(provider)
    except (ProjectConfigError, OSError) as error:
        raise DeliveryContextError("交付供应商未绑定可信项目身份") from error


def write_json_object(path: Path, value: Mapping[str, Any]) -> None:
    """原子落盘，失败时保留上次完整回执。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".delivery-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json_object(path: Path, description: str = "发布上下文") -> Dict[str, Any]:
    """读取 JSON 对象，并为发布脚本提供一致的文件与结构错误。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeliveryContextError(
            "无法读取{}：{}".format(description, path)
        ) from error
    if not isinstance(value, dict):
        raise DeliveryContextError("{}不是 JSON 对象：{}".format(description, path))
    return value


class GitTarget(NamedTuple):
    """由测试分支或正式 Tag 解析出的跨平台构建目标。"""

    ref: str
    brand: str
    version: Optional[str]


def resolve_test_branch_target(branch: str) -> GitTarget:
    """统一解析 Android/iOS 测试 Job 支持的分支命名。"""
    ref = normalize_branch(branch)
    for pattern in (FEATURE_BRANCH, DEV_BRANCH, RELEASE_BRANCH):
        match = pattern.fullmatch(ref)
        if match:
            return GitTarget(
                ref=ref,
                brand=match.group("brand"),
                version=match.groupdict().get("version"),
            )
    raise DeliveryContextError(
        "测试包分支必须使用 feature/<brand>/<summary>、dev/<brand> 或 "
        "release/<brand>-<version>，当前为：{}".format(ref or "unknown")
    )


def resolve_git_target(
    pipeline: str,
    branch: str,
    tag: str,
    error_type: Type[RuntimeError],
    *,
    brand: Optional[str] = None,
    tag_infix: str = "-v",
    message_prefix: str = "",
) -> GitTarget:
    """共享引用解析和品牌匹配，平台入口保留异常类型与 Tag 契约。"""
    from project import selected_project
    profile = selected_project()
    native = any(p.get("adapter") != "sxmqlive" for p in profile["platforms"].values())
    declared_brands = profile.get("brands", [])
    if native and brand is not None and brand not in declared_brands:
        raise error_type("品牌不属于可信项目配置")
    ref = tag.strip()
    if pipeline == "release":
        match = re.fullmatch(
            r"(?P<brand>{}){}(?P<version>{})".format(
                BRAND_PATTERN, re.escape(tag_infix), VERSION_PATTERN
            ),
            ref,
        )
        if not match or (brand is not None and match.group("brand") != brand) or (native and match.group("brand") not in declared_brands):
            expected_brand = brand if match and brand is not None else "<brand>"
            raise error_type(
                "{}正式发布 Tag 必须使用 {}{}<version>，当前为：{}".format(
                    message_prefix, expected_brand, tag_infix, ref or "unknown"
                )
            )
        return GitTarget(ref, match.group("brand"), match.group("version"))
    if pipeline != "test":
        raise error_type("Jenkins Job 类型必须为 test 或 release：{}".format(pipeline))
    if ref:
        raise error_type("{}测试包 Job 不接受 Tag：{}".format(message_prefix, ref))
    try:
        if native and normalize_branch(branch) == profile["repositories"]["application"]["defaultBranch"]:
            selected = brand or (declared_brands[0] if len(declared_brands) == 1 else None)
            if selected is None:
                raise error_type("多品牌原生项目必须显式指定 --brand")
            return GitTarget(normalize_branch(branch), selected, None)
        target = resolve_test_branch_target(branch)
    except DeliveryContextError as error:
        raise error_type("{}{}".format(message_prefix, error)) from error
    if brand is not None and target.brand != brand:
        raise error_type(
            "{}测试包分支品牌必须为 {}，当前分支为：{}".format(
                message_prefix, brand, target.ref or "unknown"
            )
        )
    if native and target.brand not in declared_brands:
        raise error_type("分支品牌不属于可信项目配置")
    return target


class WorkItemRef(NamedTuple):
    """关联的外部工作项；不包含任何办公系统认证信息。"""

    system: str
    item_type: str
    item_id: str
    url: str

    @property
    def display_name(self) -> str:
        label = WORK_ITEM_TYPE_LABELS.get(self.item_type, self.item_type)
        return "{} #{}".format(label, self.item_id)

    def to_dict(self) -> Dict[str, str]:
        return {
            "system": self.system,
            "type": self.item_type,
            "id": self.item_id,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkItemRef":
        work_item = cls(
            system=str(value.get("system") or ""),
            item_type=str(value.get("type") or ""),
            item_id=str(value.get("id") or ""),
            url=str(value.get("url") or ""),
        )
        _validate_work_item(work_item)
        return work_item


def _validate_work_item(work_item: WorkItemRef) -> None:
    if work_item.system != "zentao":
        raise DeliveryContextError("当前只支持关联 zentao 工作项")
    if work_item.item_type not in ZENTAO_WORK_ITEM_TYPES:
        raise DeliveryContextError(
            "禅道工作项类型必须为：{}".format(
                ", ".join(ZENTAO_WORK_ITEM_TYPES)
            )
        )
    if not WORK_ITEM_ID.fullmatch(work_item.item_id):
        raise DeliveryContextError("禅道工作项 ID 必须为正整数")
    parsed = urlsplit(work_item.url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise DeliveryContextError("禅道工作项链接必须为不含账号密码的 HTTPS 地址")


def optional_zentao_work_item(
    item_type: str,
    item_id: str,
    url: str,
) -> Optional[WorkItemRef]:
    """构造可选禅道引用；三项必须同时为空或同时有效。"""
    values = (item_type.strip(), item_id.strip(), url.strip())
    if not any(values):
        return None
    if not all(values):
        raise DeliveryContextError("禅道工作项类型、ID 和链接必须同时填写")
    work_item = WorkItemRef(
        system="zentao",
        item_type=values[0],
        item_id=values[1],
        url=values[2],
    )
    _validate_work_item(work_item)
    return work_item


class ArtifactInfo(NamedTuple):
    """分发后可供通知和归档使用的安装包信息。

    ``build_key`` 为兼容现有 ``release-info.json`` 保留。通知层只把它视为
    外部分发记录标识，不依赖蒲公英的具体协议。
    """

    name: str
    version: str
    version_code: str
    package_name: str
    build_key: str
    download_url: str
    qr_code_url: str
    ui_engine: str = "cmp"

    def to_dict(self) -> Dict[str, str]:
        return dict(self._asdict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactInfo":
        return cls(
            name=str(value.get("name") or ""),
            version=str(value.get("version") or ""),
            version_code=str(value.get("version_code") or ""),
            package_name=str(value.get("package_name") or ""),
            build_key=str(value.get("build_key") or ""),
            download_url=str(value.get("download_url") or ""),
            qr_code_url=str(value.get("qr_code_url") or ""),
            ui_engine=validate_ui_engine(str(value.get("ui_engine", "cmp"))),
        )


class DeliveryContext(NamedTuple):
    """跨平台构建、分发、通知共同消费的稳定发布上下文。"""

    platform: str
    brand: str
    display_name: str
    environment: str
    branch: str
    commit: str
    builder: str
    version_name: str
    version_code: str
    artifact_path: str
    changelog_path: str
    distribution: str
    delivery: str
    work_item: Optional[WorkItemRef]
    ui_engine: str = "cmp"

    def to_dict(self, extras: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """输出兼容旧脚本的扁平 JSON，并允许附加平台专属字段。"""
        result: Dict[str, Any] = dict(extras or {})
        result.update(
            {
                "platform": self.platform,
                "ui_engine": validate_ui_engine(self.ui_engine),
                "brand": self.brand,
                "display_name": self.display_name,
                "environment": self.environment,
                "branch": self.branch,
                "commit": self.commit,
                "builder": self.builder,
                "version_name": self.version_name,
                "version_code": self.version_code,
                "artifact_path": self.artifact_path,
                "changelog_path": self.changelog_path,
                "distribution": self.distribution,
                "delivery": self.delivery,
            }
        )
        if self.work_item is not None:
            _validate_work_item(self.work_item)
            result["work_item"] = self.work_item.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DeliveryContext":
        """读取新格式或仅含 ``apk_path`` / ``ipa_path`` 的历史上下文。"""
        platform = str(value.get("platform") or "").lower()
        if not platform:
            platform = "ios" if value.get("ipa_path") else "android"
        artifact_path = str(
            value.get("artifact_path")
            or value.get("ipa_path")
            or value.get("apk_path")
            or ""
        )
        work_item_value = value.get("work_item")
        work_item = (
            WorkItemRef.from_dict(work_item_value)
            if isinstance(work_item_value, dict)
            else None
        )
        context = cls(
            platform=platform,
            brand=str(value.get("brand") or ""),
            display_name=str(value.get("display_name") or ""),
            environment=str(value.get("environment") or ""),
            branch=str(value.get("branch") or "unknown"),
            commit=str(value.get("commit") or "unknown"),
            builder=str(value.get("builder") or "jenkins"),
            version_name=str(value.get("version_name") or ""),
            version_code=str(
                value.get("version_code") or value.get("build_number") or ""
            ),
            artifact_path=artifact_path,
            changelog_path=str(value.get("changelog_path") or ""),
            distribution=str(value.get("distribution") or ""),
            delivery=str(value.get("delivery") or "pgyer"),
            work_item=work_item,
            ui_engine=validate_ui_engine(str(value.get("ui_engine", "cmp"))),
        )
        missing = [
            name
            for name, field_value in (
                ("platform", context.platform),
                ("brand", context.brand),
                ("artifact_path", context.artifact_path),
                ("changelog_path", context.changelog_path),
            )
            if not field_value
        ]
        if missing:
            raise DeliveryContextError(
                "发布上下文缺少字段：{}".format(", ".join(missing))
            )
        return context


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="交付状态目录预检，不执行上传或通知")
    parser.add_argument("command", choices=("preflight", "check-state-archive", "check-state-coverage"))
    parser.add_argument("path", type=Path, nargs="?")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uid", type=int)
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--outside", type=Path)
    args = parser.parse_args(argv)
    if args.command != "preflight":
        if args.path is None or args.dry_run:
            parser.error("归档检查需要路径，不能跳过校验")
        try:
            result = (inspect_state_archive(args.path, args.uid, args.private, args.outside)
                      if args.command == "check-state-archive"
                      else validate_state_coverage(read_json_object(args.path, "回执覆盖声明")))
        except (OSError, DeliveryContextError) as error:
            print("回执归档检查失败：" + str(error))
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.path is not None:
        parser.error("preflight 不接受归档路径")
    if args.dry_run:
        print("DRY_RUN：跳过真实交付状态预检，不读取或创建回执。")
        return 0
    try:
        root = delivery_state_root()
        # 可读的目录未必可写；临时探针不创建、迁移或改写交付回执。
        with tempfile.TemporaryFile(dir=root) as probe:
            probe.write(b"preflight")
            probe.flush()
            os.fsync(probe.fileno())
    except (OSError, DeliveryContextError) as error:
        print("交付预检失败：{}".format(error))
        return 1
    print("交付状态目录预检通过：{}".format(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
