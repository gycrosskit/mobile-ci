"""节点私有设备池：仅保存已验证 Profile/成功维护批准的设备。"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from project import SOURCE_ROOT, TOOL_ROOT

ROOT = SOURCE_ROOT
PLATFORMS = {"ios", "harmony"}


class DeviceError(ValueError):
    """可安全显示的设备选择/节点目录错误。"""


def normalize_device(platform, row):
    if platform not in PLATFORMS or not isinstance(row, dict):
        raise DeviceError("设备平台或结构无效")
    name, udid = row.get("name"), row.get("udid")
    limit = 80 if platform == "ios" else 100
    pattern = r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{8}-[0-9a-fA-F]{16})" if platform == "ios" else r"[A-Za-z0-9-]{16,64}"
    if (not isinstance(name, str) or not name.strip() or len(name) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or not isinstance(udid, str) or not re.fullmatch(pattern, udid)):
        raise DeviceError("设备名称或 UDID 无效；未输出原始设备材料")
    device_type = row.get("deviceType", "IOS" if platform == "ios" else 4)
    if (platform == "harmony" and (type(device_type) is not int or device_type != 4)) or (
            platform == "ios" and device_type not in ("IOS", "IPHONE", "IPAD", "IPOD", "APPLE_TV", "APPLE_WATCH", "APPLE_VISION_PRO")):
        raise DeviceError("设备类型与平台不一致")
    return {"name": name, "udid": udid.lower() if platform == "ios" else udid,
            "deviceType": device_type}


def device_id(platform, udid):
    if platform not in PLATFORMS:
        raise DeviceError("未知设备平台")
    return hashlib.sha256((platform + ":" + udid.strip().lower()).encode()).hexdigest()[:20]


def _private(path, directory=False):
    if path.is_symlink() or not (path.is_dir() if directory else path.is_file()):
        raise DeviceError("设备池路径不能是符号链接或错误类型")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
        raise DeviceError("设备池目录须为 0700，文件须为 0600 且属于当前用户")


def _directory(state_root, create=False):
    root = Path(state_root)
    if not root.is_absolute() or root.resolve() != root:
        raise DeviceError("设备池根目录须为无符号链接的绝对路径")
    _private(root, True)
    workspace = Path(os.environ.get("WORKSPACE", ROOT)).resolve()
    if any(root == parent or parent in root.parents for parent in (ROOT, TOOL_ROOT, workspace)):
        raise DeviceError("设备池必须位于 workspace/仓库外")
    directory = root / "devices"
    if not directory.exists() and not directory.is_symlink():
        if not create:
            return None
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
    _private(directory, True)
    return directory


def _source(source):
    if (not isinstance(source, dict) or set(source) != {"brand", "team", "profile"}
            or any(not isinstance(value, str) or not value.strip() or len(value) > 200
                   or any(ord(char) < 32 for char in value) for value in source.values())):
        raise DeviceError("设备批准来源必须包含品牌、Team 和已验证 Profile")
    return dict(source)


def _read(directory, platform):
    if platform not in PLATFORMS:
        raise DeviceError("未知设备平台")
    if directory is None:
        return []
    lock = directory / (platform + ".lock")
    if lock.exists() or lock.is_symlink():
        _private(lock)
    path = directory / (platform + ".json")
    if not path.exists() and not path.is_symlink():
        return []
    _private(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"platform", "devices"} or value["platform"] != platform or not isinstance(value["devices"], list):
        raise DeviceError("设备池内容与平台不一致")
    result, seen = [], set()
    for row in value["devices"]:
        if not isinstance(row, dict) or set(row) != {"id", "name", "udid", "deviceType", "sources"}:
            raise DeviceError("设备池记录结构无效")
        device = normalize_device(platform, row)
        identity = device_id(platform, device["udid"])
        if row["id"] != identity or identity in seen or not isinstance(row["sources"], list) or not row["sources"]:
            raise DeviceError("设备池 ID 或批准来源无效")
        sources = [_source(source) for source in row["sources"]]
        result.append(dict(device, id=identity, sources=sources))
        seen.add(identity)
    return result


def _public(rows):
    def safe(value):
        for device in rows:
            value = re.sub(re.escape(device["udid"]), "[设备标识已隐藏]", value, flags=re.IGNORECASE)
        return value
    return [{"id": row["id"], "name": safe(row["name"]), "maskedUdid": row["udid"][:4] + "…" + row["udid"][-4:],
             "deviceType": row["deviceType"], "sources": [{key: safe(value) for key, value in source.items()}
                                                          for source in row["sources"]]} for row in rows]


def public_catalog(state_root, platform):
    return _public(_read(_directory(state_root), platform))


load_catalog = public_catalog


@contextlib.contextmanager
def _locked(directory, platform):
    path = directory / (platform + ".lock")
    if path.exists() or path.is_symlink():
        _private(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        _private(path)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def merge_catalog(state_root, platform, devices, source):
    if platform not in PLATFORMS or not isinstance(devices, list) or len(devices) > 1000:
        raise DeviceError("设备池平台或设备数量无效")
    source = _source(source)
    approved = [normalize_device(platform, row) for row in devices]
    directory = _directory(state_root, True)
    with _locked(directory, platform):
        rows = {row["id"]: row for row in _read(directory, platform)}
        for device in approved:
            identity = device_id(platform, device["udid"])
            existing = rows.setdefault(identity, dict(device, id=identity, sources=[]))
            if source not in existing["sources"]:
                existing["sources"].append(source)
        ordered = sorted(rows.values(), key=lambda row: row["id"])
        path = directory / (platform + ".json")
        if path.exists() or path.is_symlink():
            _private(path)
        fd, temporary = tempfile.mkstemp(prefix="." + platform + "-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"platform": platform, "devices": ordered}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return _public(ordered)


def select_devices(state_root, platform, source="manual", ids="all", name="", udid=""):
    if bool(name) != bool(udid):
        raise DeviceError("设备输入必须同时填写 DEVICE_NAME 和 DEVICE_UDID")
    if source not in ("manual", "shared"):
        raise DeviceError("DEVICE_SOURCE 必须为 shared 或 manual")
    if source == "manual":
        if ids != "all":
            raise DeviceError("手动设备来源不能同时选择 DEVICE_IDS")
        return [normalize_device(platform, {"name": name, "udid": udid})] if name else []
    if name or udid:
        raise DeviceError("shared 与手动 DEVICE_NAME/DEVICE_UDID 冲突，请清空手动输入或改选 manual")
    rows = _read(_directory(state_root), platform)
    if not rows:
        raise DeviceError("所选平台批准设备池为空；请改选 manual 并填写设备名和 UDID")
    if ids == "all":
        selected = rows
    else:
        chosen = {value.strip() for value in ids.split(",")}
        known = {row["id"] for row in rows}
        if not chosen or not chosen <= known:
            raise DeviceError("DEVICE_IDS 包含未知或其他平台设备 ID")
        selected = [row for row in rows if row["id"] in chosen]
    return [{key: row[key] for key in ("name", "udid", "deviceType")} for row in selected]
