"""离线安装器的真实 ZIP 拒绝路径；不联网、不签名。"""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('ohos_clt', ROOT / 'jenkins/remote/ohos-agent/clt.py')
clt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(clt)


def member(name, mode=stat.S_IFREG | 0o644):
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = mode << 16
    return info


def fixture():
    files = {'LICENSE.txt': b'test license', 'version.txt': b'# Version: 26.0.0.851\n'}
    for path, version in (('hvigor/hvigor/package.json', '6.26.8'),
                          ('hvigor/hvigor-ohos-plugin/package.json', '6.26.8'),
                          ('ohpm/package.json', '26.0.0.630')):
        files[path] = json.dumps({'version': version}).encode()
    for namespace, manifest in (('openharmony', 'oh-uni-package.json'), ('hms', 'uni-package.json')):
        for component in ('native', 'ets', 'toolchains', 'js', 'previewer'):
            files['sdk/default/' + namespace + '/' + component + '/' + manifest] = json.dumps({
                'apiVersion': '26', 'version': '26.0.0.105'}).encode()
    jar = io.BytesIO()
    with zipfile.ZipFile(jar, 'w') as archive:
        archive.writestr('META-INF/MANIFEST.MF', 'Manifest-Version: 1.0')
    for name in clt.SIGNING_JARS:
        files['sdk/default/openharmony/toolchains/lib/' + name] = jar.getvalue()
    for name in clt.HMS_SIGNING_JARS:
        files['sdk/default/hms/toolchains/lib/' + name] = jar.getvalue()
    for name in ('tool/node/bin/node', 'bin/hvigorw', 'ohpm/bin/ohpm',
                 'sdk/default/openharmony/native/llvm/bin/clang'):
        files[name] = b'#!/bin/sh\nexit 0\n'
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        for name, content in files.items():
            archive.writestr(member('clt/' + name, stat.S_IFREG | (0o755 if content.startswith(b'#!') else 0o644)), content)
        archive.writestr(member('clt/tool/node/bin/npm', stat.S_IFLNK | 0o777), 'node')
    return data.getvalue()


class OhosRebuildTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def unpack(self, entries):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            for name, content in entries:
                archive.writestr(name if isinstance(name, zipfile.ZipInfo) else member(name), content)
        data.seek(0)
        stage = self.root / 'stage'
        stage.mkdir(exist_ok=True)
        with zipfile.ZipFile(data) as archive:
            return clt.unpack(archive, stage)

    def test_install_receipt_versions_safe_links_and_no_overwrite(self):
        data = fixture()
        source = self.root / 'official.zip'
        source.write_bytes(data)
        destination = self.root / 'installed'
        with patch.object(clt, 'SHA256', hashlib.sha256(data).hexdigest()), patch.object(
                clt, 'LICENSE_SHA256', hashlib.sha256(b'test license').hexdigest()):
            clt.install(source, destination, True)
            clt.check_tree(destination)
            self.assertTrue((destination / 'tool/node/bin/npm').is_symlink())
            receipt = json.loads((destination / 'ci-install-receipt.json').read_text())
            self.assertEqual(clt.SHA256, receipt['archiveSha256'])
            with self.assertRaisesRegex(ValueError, 'existing'):
                clt.install(source, destination, True)
            self.assertTrue((destination / 'LICENSE.txt').exists())
            (destination / 'hvigor/hvigor/package.json').write_text('{"version":"wrong"}')
            with self.assertRaisesRegex(ValueError, 'tool version'):
                clt.check_tree(destination)

    def test_license_and_checksum_fail_closed_without_destination(self):
        archive = self.root / 'wrong.zip'
        archive.write_bytes(fixture())
        destination = self.root / 'installed'
        with self.assertRaisesRegex(ValueError, 'license'):
            clt.install(archive, destination)
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            clt.install(archive, destination, True)
        self.assertFalse(destination.exists())
        self.assertEqual(['wrong.zip'], [item.name for item in self.root.iterdir()])

    def test_invalid_sdk_never_publishes_a_directory(self):
        rewritten = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(fixture())) as original, zipfile.ZipFile(rewritten, 'w') as archive:
            for info in original.infolist():
                archive.writestr(info, original.read(info).replace(b'26.0.0.105', b'26.0.0.999'))
        data = rewritten.getvalue()
        source = self.root / 'bad-sdk.zip'
        source.write_bytes(data)
        with patch.object(clt, 'SHA256', hashlib.sha256(data).hexdigest()), patch.object(
                clt, 'LICENSE_SHA256', hashlib.sha256(b'test license').hexdigest()):
            with self.assertRaisesRegex(ValueError, 'SDK version'):
                clt.install(source, self.root / 'installed', True)
        self.assertEqual(['bad-sdk.zip'], [item.name for item in self.root.iterdir()])

    def test_path_traversal_absolute_windows_and_multiple_roots(self):
        for name in ('../outside', '/outside', 'clt/../outside', 'clt/./outside',
                     'clt//outside', 'clt\\outside', 'C:/outside'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.unpack([(name, b'bad')])
        with self.assertRaisesRegex(ValueError, 'one CLT root'):
            self.unpack([('clt/file', b'x'), ('other/file', b'x')])
        self.assertFalse((self.root / 'outside').exists())

    def test_official_clang_chain_is_folded_to_regular_member_before_links_are_created(self):
        base = 'clt/sdk/default/hms/native/BiSheng/bin/'
        tree = self.unpack([
            (member(base + 'clang++', stat.S_IFLNK | 0o777), 'bisheng-clang++'),
            (member(base + 'bisheng-clang++', stat.S_IFLNK | 0o777), 'clang'),
            (member(base + 'clang', stat.S_IFLNK | 0o777), 'bisheng-clang'),
            (member(base + 'bisheng-clang', stat.S_IFLNK | 0o777), 'clang-15'),
            (member(base + 'clang-15', stat.S_IFREG | 0o755), b'official executable'),
        ])
        directory = tree / 'sdk/default/hms/native/BiSheng/bin'
        for name in ('clang++', 'bisheng-clang++', 'clang', 'bisheng-clang'):
            link = directory / name
            self.assertTrue(link.is_symlink())
            self.assertEqual('clang-15', str(link.readlink()))
            self.assertEqual(b'official executable', link.read_bytes())
            self.assertEqual(0o755, stat.S_IMODE(link.stat().st_mode))

    def test_links_cannot_escape_dangle_cycle_target_directories_or_be_used_as_parent(self):
        for target in ('../../outside', '../outside', '/outside', 'missing', 'link', '..'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.unpack([(member('clt/link', stat.S_IFLNK | 0o777), target),
                             (member('clt/link2', stat.S_IFLNK | 0o777), 'file'),
                             ('clt/file', b'x')])
        with self.assertRaisesRegex(ValueError, 'cycle'):
            self.unpack([(member('clt/link', stat.S_IFLNK | 0o777), 'link2'),
                         (member('clt/link2', stat.S_IFLNK | 0o777), 'link')])
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.unpack([(member('clt/link', stat.S_IFLNK | 0o777), 'link2'),
                         (member('clt/link2', stat.S_IFLNK | 0o777), 'missing')])
        with self.assertRaisesRegex(ValueError, 'regular member'):
            self.unpack([(member('clt/link', stat.S_IFLNK | 0o777), 'dir'),
                         (member('clt/dir/', stat.S_IFDIR | 0o755), b'')])
        with self.assertRaisesRegex(ValueError, 'parent'):
            self.unpack([(member('clt/link', stat.S_IFLNK | 0o777), 'file'),
                         ('clt/link/child', b'x'), ('clt/file', b'x')])
        self.assertFalse(any((self.root / 'stage').iterdir()))

    def test_link_depth_and_total_resolution_budget_fail_before_extraction(self):
        chain = [(member('clt/link', stat.S_IFLNK | 0o777), 'link2'),
                 (member('clt/link2', stat.S_IFLNK | 0o777), 'link3'),
                 (member('clt/link3', stat.S_IFLNK | 0o777), 'file'), ('clt/file', b'x')]
        with patch.object(clt, 'MAX_LINK_DEPTH', 2), self.assertRaisesRegex(ValueError, 'resolution limit'):
            self.unpack(chain)
        with patch.object(clt, 'MAX_LINK_STEPS', 3), self.assertRaisesRegex(ValueError, 'resolution limit'):
            self.unpack(chain)
        self.assertFalse(any((self.root / 'stage').iterdir()))

    def test_special_privileged_duplicate_and_bomb_members(self):
        for mode in (stat.S_IFIFO | 0o644, stat.S_IFCHR | 0o644, stat.S_IFREG | 0o4755):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.unpack([(member('clt/file', mode), b'x')])
        with self.assertWarns(UserWarning), self.assertRaisesRegex(ValueError, 'duplicate'):
            self.unpack([('clt/file', b'x'), ('clt/file', b'y')])
        with patch.object(clt, 'MAX_FILES', 1), self.assertRaisesRegex(ValueError, 'member limit'):
            self.unpack([('clt/a', b'x'), ('clt/b', b'y')])
        with patch.object(clt, 'MAX_BYTES', 10), self.assertRaisesRegex(ValueError, 'expansion limit'):
            self.unpack([('clt/a', b'x' * 11)])
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('clt/zeros', b'\0' * 1024**2)
        with zipfile.ZipFile(data) as archive, self.assertRaisesRegex(ValueError, 'expansion limit'):
            clt.unpack(archive, self.root)

    def test_crc_failure_is_not_accepted(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr(member('clt/file'), b'unique-payload')
        corrupted = data.getvalue().replace(b'unique-payload', b'broken-payload', 1)
        with zipfile.ZipFile(io.BytesIO(corrupted)) as archive, self.assertRaises(zipfile.BadZipFile):
            clt.unpack(archive, self.root)

    def test_sdk_component_and_signing_tool_are_required(self):
        with zipfile.ZipFile(io.BytesIO(fixture())) as archive:
            tree = clt.unpack(archive, self.root)
        with patch.object(clt, 'LICENSE_SHA256', hashlib.sha256(b'test license').hexdigest()):
            missing = tree / 'sdk/default/openharmony/toolchains/lib/hap-sign-tool.jar'
            missing.unlink()
            with self.assertRaisesRegex(ValueError, 'signing/packing'):
                clt.check_tree(tree)

    def test_root_runtime_and_source_links_are_rejected(self):
        source = self.root / 'official.zip'
        source.write_bytes(fixture())
        link = self.root / 'source-link'
        link.symlink_to(source)
        with self.assertRaises(OSError):
            clt.install(link, self.root / 'installed', True)
        with zipfile.ZipFile(source) as archive:
            tree = clt.unpack(archive, self.root)
        with patch.object(clt, 'LICENSE_SHA256', hashlib.sha256(b'test license').hexdigest()), patch.object(
                clt.os, 'getuid', return_value=0), self.assertRaisesRegex(ValueError, 'root'):
            clt.check_tree(tree, runtime=True)


if __name__ == '__main__':
    unittest.main()
