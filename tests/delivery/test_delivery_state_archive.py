import io
import json
import os
import subprocess
import sys
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from delivery.delivery import DeliveryContextError, inspect_state_archive, validate_state_coverage


class DeliveryStateArchiveTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.path = self.root / "state.tar.gz"

    def archive(self, extra=None, root_mode=0o700, uid=1000, receipt=True):
        with tarfile.open(self.path, "w:gz") as archive:
            root = tarfile.TarInfo("delivery-state")
            root.type, root.mode, root.uid = tarfile.DIRTYPE, root_mode, uid
            archive.addfile(root)
            if receipt:
                value = json.dumps({"version": 1, "identity": {"operation": "fixture"}, "phase": "pending"}).encode()
                item = tarfile.TarInfo("delivery-state/" + "a" * 64 + ".json")
                item.mode, item.uid, item.size = 0o600, uid, len(value)
                archive.addfile(item, io.BytesIO(value))
            if extra is not None:
                archive.addfile(extra, io.BytesIO(b"x" * extra.size) if extra.isfile() else None)
        self.path.chmod(0o600)

    def test_valid_private_archive_reports_metadata_and_preserves_bytes(self):
        self.archive(uid=502)
        before = self.path.read_bytes()
        result = inspect_state_archive(self.path, expected_uid=502, private=True, outside=self.root / "home")
        self.assertEqual((502, 1, 1), (result["uid"], result["files"], result["receipts"]))
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(64, len(result["sha256"]))

    def test_root_only_is_not_claimed_to_contain_delivery_history(self):
        self.archive(receipt=False)
        self.assertEqual(0, inspect_state_archive(self.path)["receipts"])

    def test_links_traversal_special_nodes_and_duplicates_are_rejected(self):
        for name, kind in [("../outside", tarfile.REGTYPE), ("delivery-state/other", tarfile.REGTYPE),
                           ("delivery-state/" + "a" * 64 + ".json", tarfile.REGTYPE),
                           ("delivery-state/" + "b" * 64 + ".json", tarfile.SYMTYPE),
                           ("delivery-state/" + "b" * 64 + ".json", tarfile.LNKTYPE),
                           ("delivery-state/pipe", tarfile.FIFOTYPE)]:
            with self.subTest(name=name, kind=kind):
                item = tarfile.TarInfo(name)
                item.type, item.mode, item.uid = kind, 0o600, 1000
                item.linkname = "../../outside"
                self.archive(item)
                with self.assertRaises(DeliveryContextError):
                    inspect_state_archive(self.path)

    def test_mode_owner_count_and_size_bounds_are_enforced(self):
        self.archive(root_mode=0o755)
        with self.assertRaises(DeliveryContextError):
            inspect_state_archive(self.path)
        self.archive(uid=502)
        with self.assertRaises(DeliveryContextError):
            inspect_state_archive(self.path, expected_uid=1000)
        self.archive()
        for field, maximum in [("STATE_ARCHIVE_FILES", 0), ("STATE_ARCHIVE_LIMIT", 1)]:
            with self.subTest(field=field), patch("delivery.delivery." + field, maximum):
                with self.assertRaises(DeliveryContextError):
                    inspect_state_archive(self.path)

    def test_private_input_rejects_symlink_hardlink_wrong_mode_and_home(self):
        self.archive()
        linked = self.root / "link.tar.gz"
        linked.symlink_to(self.path)
        with self.assertRaises(DeliveryContextError):
            inspect_state_archive(linked, private=True)
        os.link(self.path, self.root / "hardlink.tar.gz")
        with self.assertRaises(DeliveryContextError):
            inspect_state_archive(self.path, private=True)
        (self.root / "hardlink.tar.gz").unlink()
        self.path.chmod(0o644)
        with self.assertRaises(DeliveryContextError):
            inspect_state_archive(self.path, private=True)
        self.path.chmod(0o600)
        with self.assertRaises(DeliveryContextError):
            inspect_state_archive(self.path, private=True, outside=self.root)
        self.root.chmod(0o755)
        with self.assertRaises(DeliveryContextError):
            inspect_state_archive(self.path, private=True)

    def test_private_fifo_is_rejected_without_waiting_for_a_writer(self):
        os.mkfifo(str(self.path), 0o600)
        code = """
import sys
from pathlib import Path
from delivery.delivery import DeliveryContextError, inspect_state_archive
try:
    inspect_state_archive(Path(sys.argv[1]), private=True)
except DeliveryContextError:
    raise SystemExit(0)
raise SystemExit(2)
"""
        result = subprocess.run([sys.executable, '-c', code, str(self.path)],
                                cwd=str(Path(__file__).resolve().parents[2]),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
        self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', errors='replace'))

    def test_corrupt_gzip_and_invalid_receipt_are_rejected_without_reading_outside(self):
        self.path.write_bytes(b"broken")
        with self.assertRaises(DeliveryContextError):
            inspect_state_archive(self.path)
        item = tarfile.TarInfo("delivery-state/" + "b" * 64 + ".json")
        item.mode, item.uid, item.size = 0o600, 1000, 1
        self.archive(item)
        with self.assertRaises(DeliveryContextError):
            inspect_state_archive(self.path)

    def test_coverage_names_version_and_mac_uid_are_exact(self):
        value = {"version": 1, "linux": "linux-delivery-state.tar.gz", "mac": None, "macSourceUid": None}
        self.assertEqual(value, validate_state_coverage(value))
        complete = dict(value, mac="mac-delivery-state.tar.gz", macSourceUid=502)
        self.assertEqual(complete, validate_state_coverage(complete))
        for invalid in [dict(value, version=True), dict(value, linux="../other.tar.gz"),
                        dict(value, macSourceUid=502), dict(complete, macSourceUid=0), dict(value, extra=True)]:
            with self.subTest(value=invalid), self.assertRaises(DeliveryContextError):
                validate_state_coverage(invalid)
