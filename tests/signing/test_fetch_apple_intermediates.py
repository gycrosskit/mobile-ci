import copy
import hashlib
from functools import lru_cache
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from signing import fetch_apple_intermediates as fetch

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / 'signing/resources/apple-intermediate-certificates'
SCRIPT = ROOT / 'signing/ios_apple_intermediate_certificates.sh'


@lru_cache(maxsize=1)
def fixtures():
    # 独立生成测试 CA，公开源码测试不依赖被排除的 Apple PEM 材料。
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        result = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:1024', '-nodes',
                                 '-keyout', str(root / 'key.pem'), '-outform', 'DER', '-days', '1',
                                 '-subj', '/CN=Local CA fixture'], capture_output=True, check=True, timeout=15)
    return {entry[3]: result.stdout for entry in fetch.manifest_entries()}


def fixture_entries():
    entries = fetch.manifest_entries()
    for entry in entries:
        entry[1] = hashlib.sha256(fixtures()[entry[3]]).hexdigest()
    return entries


def shell_fixture(root):
    source = root / 'signing'
    bundle = source / 'resources/apple-intermediate-certificates'
    bundle.mkdir(parents=True)
    shutil.copy2(SCRIPT, source / SCRIPT.name)
    (bundle / 'manifest.txt').write_text('\n'.join(' '.join(entry) for entry in fixture_entries()))
    return source / SCRIPT.name


class FetchAppleIntermediatesTest(unittest.TestCase):
    def test_all_seven_verified_then_publish_and_existing_verify_uses_trusted_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            output = root / 'certificates'
            data = fixtures()
            with patch.object(fetch, 'manifest_entries', return_value=fixture_entries()), patch.object(fetch, 'download', side_effect=lambda url: data[url]) as download:
                fetch.fetch(output)
            self.assertEqual(7, download.call_count)
            self.assertEqual(7, len(list(output.glob('*.pem'))))
            # 外部目录里的 manifest 不能替换 source 中受信 pins。
            (output / 'manifest.txt').write_text('forged manifest')
            script = shell_fixture(root)
            result = subprocess.run(['bash', str(script), 'verify'], capture_output=True, text=True,
                                    env=dict(os.environ, APPLE_INTERMEDIATE_CERTIFICATE_DIR=str(output)), timeout=20)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('Verified 7 pinned', result.stdout)
            self.assertEqual({'certificates', 'signing'}, {path.name for path in root.iterdir()})

    def test_digest_malformed_der_and_partial_failure_leave_no_output(self):
        data = list(fixtures().values())
        for error in ('digest', 'parse', 'partial'):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); output = root / 'certificates'
                values = copy.deepcopy(data)
                entries = fixture_entries()
                if error == 'digest': values[0] = b'not the pinned certificate'
                if error == 'partial': values[3] = OSError('download unavailable')
                if error == 'parse':
                    values[0] = b'not DER'; entries[0][1] = hashlib.sha256(values[0]).hexdigest()
                with patch.object(fetch, 'manifest_entries', return_value=entries), patch.object(fetch, 'download', side_effect=values):
                    with self.assertRaises((OSError, ValueError, subprocess.SubprocessError)): fetch.fetch(output)
                self.assertFalse(output.exists())
                self.assertEqual([], list(root.iterdir()))

    def test_manifest_rejects_untrusted_urls_paths_duplicates_links_and_special_files(self):
        original = (BUNDLE / 'manifest.txt').read_text()
        urls = ('https://evil.example/AppleWWDRCAG2.cer',
                'http://www.apple.com/certificateauthority/AppleWWDRCAG2.cer',
                'https://www.apple.com/certificateauthority/../AppleWWDRCAG2.cer',
                'https://www.apple.com/certificateauthority/AppleWWDRCAG2.cer?redirect=evil',
                'https://www.apple.com@evil.example/certificateauthority/AppleWWDRCAG2.cer')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); manifest = root / 'manifest.txt'
            for content in [original.replace(fetch.manifest_entries()[0][3], url) for url in urls] + [original.replace('AppleWWDRCAG2.pem', '../outside.pem'), original + original]:
                manifest.write_text(content)
                with patch.object(fetch, 'MANIFEST', manifest), self.assertRaises(ValueError): fetch.manifest_entries()
            manifest.write_text(original)
            link = root / 'link'; link.symlink_to(manifest)
            with patch.object(fetch, 'MANIFEST', link), self.assertRaises(ValueError): fetch.manifest_entries()
            hard = root / 'hard'; os.link(manifest, hard)
            with patch.object(fetch, 'MANIFEST', hard), self.assertRaises(ValueError): fetch.manifest_entries()
            hard.unlink(); manifest.chmod(0o666)
            with patch.object(fetch, 'MANIFEST', manifest), self.assertRaises(ValueError): fetch.manifest_entries()
            fifo = root / 'fifo'; os.mkfifo(fifo)
            with patch.object(fetch, 'MANIFEST', fifo), self.assertRaises(ValueError): fetch.manifest_entries()

    def test_output_and_parent_links_existing_directory_and_race_never_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); output = root / 'certificates'; output.mkdir()
            marker = output / 'keep'; marker.write_text('existing')
            with patch.object(fetch, 'download') as download:
                with self.assertRaises(ValueError): fetch.fetch(output)
                download.assert_not_called()
            linked = root / 'linked'; linked.symlink_to(output, target_is_directory=True)
            for path in (linked, linked / 'new-output', root / 'missing-parent' / 'new-output'):
                with self.assertRaises(ValueError): fetch.fetch(path)
            output2 = root / 'new-output'
            data = fixtures()
            def download_and_race(url):
                if not output2.exists():
                    output2.mkdir(); (output2 / 'keep').write_text('race')
                return data[url]
            with patch.object(fetch, 'manifest_entries', return_value=fixture_entries()), patch.object(fetch, 'download', side_effect=download_and_race), self.assertRaises(OSError): fetch.fetch(output2)
            self.assertEqual('existing', marker.read_text())
            self.assertEqual('race', (output2 / 'keep').read_text())
            self.assertEqual({'certificates', 'linked', 'new-output'}, {path.name for path in root.iterdir()})
            empty = root / 'empty-race'; inode = None
            def create_empty_race(url):
                nonlocal inode
                if not empty.exists():
                    empty.mkdir(); inode = empty.stat().st_ino
                return data[url]
            with patch.object(fetch, 'manifest_entries', return_value=fixture_entries()), patch.object(fetch, 'download', side_effect=create_empty_race), self.assertRaises(OSError):
                fetch.fetch(empty)
            self.assertEqual(inode, empty.stat().st_ino)
            self.assertEqual([], list(empty.iterdir()))

    def test_download_enforces_tls_bounds_no_redirect_and_body_limit(self):
        for body in (b'der\n200', b'der\n302', b'\n200', b'x' * (fetch.MAX_CERTIFICATE_BYTES + 1) + b'\n200'):
            with patch.object(fetch.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, body, b'')) as run:
                if body == b'der\n200':
                    self.assertEqual(b'der', fetch.download('https://www.apple.com/certificateauthority/AppleWWDRCAG2.cer'))
                else:
                    with self.assertRaises(ValueError): fetch.download('https://www.apple.com/certificateauthority/AppleWWDRCAG2.cer')
                args = run.call_args.args[0]
                self.assertEqual(['curl', '--disable'], args[:2])
                self.assertNotIn('--insecure', args); self.assertNotIn('--location', args)
                self.assertEqual('=https', args[args.index('--proto') + 1])
                self.assertEqual('20', args[args.index('--max-time') + 1])
                self.assertEqual(str(fetch.MAX_CERTIFICATE_BYTES), args[args.index('--max-filesize') + 1])
                self.assertEqual(25, run.call_args.kwargs['timeout'])

    def test_external_shell_directory_rejects_certificate_and_parent_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); output = root / 'certificates'
            output.mkdir()
            script = shell_fixture(root)
            for entry in fixture_entries():
                pem = subprocess.run(['openssl', 'x509', '-inform', 'DER', '-outform', 'PEM'],
                                     input=fixtures()[entry[3]], capture_output=True, check=True).stdout
                (output / entry[2]).write_bytes(pem)
                (script.parent / 'resources/apple-intermediate-certificates' / entry[2]).write_bytes(pem)
            certificate = output / 'AppleWWDRCAG2.pem'
            original = root / 'original.pem'; certificate.rename(original); certificate.symlink_to(original)
            for path in (output, root / 'linked'):
                if path.name == 'linked': path.symlink_to(output, target_is_directory=True)
                result = subprocess.run(['bash', str(script), 'verify'], capture_output=True, text=True,
                                        env=dict(os.environ, APPLE_INTERMEDIATE_CERTIFICATE_DIR=str(path)), timeout=20)
                self.assertNotEqual(0, result.returncode)
                self.assertIn('symbolic links', result.stderr)
            result = subprocess.run(['bash', str(script), 'verify'], capture_output=True, text=True,
                                    env={key: value for key, value in os.environ.items() if key != 'APPLE_INTERMEDIATE_CERTIFICATE_DIR'}, timeout=20)
            self.assertEqual(0, result.returncode, result.stderr)


if __name__ == '__main__':
    unittest.main()
