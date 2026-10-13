import io
import http.client
import argparse
import contextlib
import json
import multiprocessing
import os
import plistlib
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request

from delivery import pgyer_upload as upload
from delivery import delivery
import project_config
from delivery.delivery import ArtifactInfo, DeliveryContextError, artifact_sha256, delivery_operation
from delivery.pgyer_upload import (
    PgyerError,
    _request_json,
    get_cos_token,
    release_info_from_api,
    upload_file,
    upload_artifact,
    upload_harmony_certificate,
    wait_for_release,
    write_release_info,
)


def concurrent_upload(artifact, state_root, start, posts, result, certificate, queries=None, tokens=None, lock_attempts=None):
    original_flock = delivery.fcntl.flock
    def flock(descriptor, operation):
        if lock_attempts is not None and operation == delivery.fcntl.LOCK_EX:
            lock_attempts.put('locking')
        return original_flock(descriptor, operation)

    def send(*args):
        with posts.get_lock():
            posts.value += 1
        return (200, b'{"code":0}') if certificate else None

    release = ArtifactInfo('fixture', '1.0', '1', 'com.example', 'key', 'https://www.pgyer.com/abc', '')
    def query(*args, **kwargs):
        if queries is not None:
            with queries.get_lock():
                queries.value += 1
        return release

    def token(*args, **kwargs):
        if tokens is not None:
            with tokens.get_lock():
                tokens.value += 1
        return 'https://example.test', 'key', {}

    try:
        with patch.dict(os.environ, {'DELIVERY_STATE_ROOT': state_root}), \
                patch.object(project_config, 'PROJECTS_DIR', Path(os.environ['TEST_PGYER_PROFILES_DIR'])), \
                patch.object(delivery.fcntl, 'flock', side_effect=flock), \
                patch.object(upload, 'get_cos_token', side_effect=token), \
                patch.object(upload, 'upload_file', side_effect=send), \
                patch.object(upload, '_post_multipart', side_effect=send), \
                patch.object(upload, 'wait_for_release', side_effect=query):
            start.wait(timeout=10)
            if certificate:
                upload_harmony_certificate('fixture', 'key', Path(artifact), 'fixture')
            else:
                upload_artifact(artifact, 'fixture', 'notes', platform='harmonyos')
            result.put('ok')
    except Exception as error:
        result.put(type(error).__name__)


class PgyerUploadTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        profile = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/legacy-project.json').read_text())
        profile.pop('testOnly')
        profile['providers']['pgyer'].update(legacyNamespace=True, accountId='sxmqlive-pgyer')
        profiles = self.root / 'projects'
        profiles.mkdir()
        (profiles / 'sxmqlive.json').write_text(json.dumps(profile))
        selected = patch.object(project_config, 'PROJECTS_DIR', profiles)
        selected.start(); self.addCleanup(selected.stop)
        self.state_root = self.root / 'state'
        self.environment = patch.dict(os.environ, {'DELIVERY_STATE_ROOT': str(self.state_root), 'TEST_PGYER_PROFILES_DIR': str(profiles)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.hap = self.root / 'signed.hap'
        self.hap.write_bytes(b'hap fixture')
        self.p12 = self.root / 'release.p12'
        self.p12.write_bytes(b'private fixture')
        self.release = ArtifactInfo('fixture', '1.0', '1', 'com.example', 'key', 'https://www.pgyer.com/abc', '')

    def seed_uploaded(self, artifact, platform, upload_key):
        identity = {'operation': 'pgyer-upload', 'platform': platform, 'brand': '',
                    'artifact_sha256': artifact_sha256(artifact), 'channel_shortcut': ''}
        with delivery_operation(identity, scope={key: identity[key] for key in ('operation', 'platform', 'brand')}) as (_, save):
            save(phase='uploaded', build_key=upload_key)
        return next(Path(os.environ['DELIVERY_STATE_ROOT']).glob('*.json'))

    @patch('delivery.pgyer_upload.wait_for_release')
    @patch('delivery.pgyer_upload.upload_file')
    @patch('delivery.pgyer_upload.get_cos_token')
    def test_platform_storage_suffix_resumes_by_query_then_complete_uses_zero_network(self, token, send, query):
        for platform, suffix in [('android', '.apk'), ('ios', '.ipa'), ('harmonyos', '.hap')]:
            for strip_suffix in (False, True):
                with self.subTest(platform=platform, stripped=strip_suffix), patch.dict(os.environ, {
                        'DELIVERY_STATE_ROOT': str(self.root / (platform + str(strip_suffix)))}):
                    artifact = self.root / ('signed' + suffix)
                    artifact.write_bytes(b'original signed artifact')
                    storage_key = 'a' * 32 + suffix
                    published_key = 'a' * 32 if strip_suffix else storage_key
                    release = self.release._replace(build_key=published_key)
                    record_path = self.seed_uploaded(artifact, platform, storage_key)
                    query.reset_mock(); query.return_value = release
                    self.assertEqual(release, upload_artifact(str(artifact), 'fixture', 'notes', platform))
                    record = json.loads(record_path.read_text())
                    self.assertEqual(('complete', published_key, storage_key),
                                     (record['phase'], record['build_key'], record['upload_key']))
                    self.assertEqual(published_key, record['receipt']['build_key'])
                    self.assertEqual(release, upload_artifact(str(artifact), 'fixture', 'notes', platform))
                    query.assert_called_once()
                    self.assertEqual(storage_key, query.call_args.kwargs['build_key'])
        token.assert_not_called(); send.assert_not_called()

    @patch('delivery.pgyer_upload.wait_for_release')
    @patch('delivery.pgyer_upload.upload_file')
    @patch('delivery.pgyer_upload.get_cos_token', return_value=('https://example.test', 'key.apk', {}))
    def test_new_upload_queries_storage_key_and_saves_canonical_complete_key(self, token, send, query):
        artifact = self.root / 'signed.apk'
        artifact.write_bytes(b'original signed apk')
        query.return_value = self.release
        self.assertEqual(self.release, upload_artifact(str(artifact), 'fixture', 'notes', 'android'))
        self.assertEqual(self.release, upload_artifact(str(artifact), 'fixture', 'notes', 'android'))
        record = json.loads(next(self.state_root.glob('*.json')).read_text())
        self.assertEqual(('complete', 'key', 'key.apk'), (record['phase'], record['build_key'], record['upload_key']))
        self.assertEqual('key.apk', query.call_args.kwargs['build_key'])
        token.assert_called_once(); send.assert_called_once(); query.assert_called_once()

    @patch('delivery.pgyer_upload.wait_for_release')
    @patch('delivery.pgyer_upload.upload_file')
    @patch('delivery.pgyer_upload.get_cos_token')
    def test_wrong_key_suffix_or_middle_suffix_preserves_uploaded_state(self, token, send, query):
        for platform, suffix in [('android', '.apk'), ('ios', '.ipa'), ('harmonyos', '.hap')]:
            wrong_suffix = '.ipa' if suffix != '.ipa' else '.apk'
            pairs = [('key' + suffix, 'wrong-key'), ('key.wrong', 'key'), ('key' + wrong_suffix, 'key'),
                     ('key' + suffix + '.tmp', 'key.tmp'), ('key' + suffix + suffix, 'key'),
                     ('key' + suffix.upper(), 'key')]
            for index, (storage_key, published_key) in enumerate(pairs):
                with self.subTest(platform=platform, keys=(storage_key, published_key)), patch.dict(os.environ, {
                        'DELIVERY_STATE_ROOT': str(self.root / (platform + '-wrong-' + str(index)))}):
                    artifact = self.root / ('signed' + suffix)
                    artifact.write_bytes(b'original signed artifact')
                    record_path = self.seed_uploaded(artifact, platform, storage_key)
                    before = record_path.read_bytes()
                    query.return_value = self.release._replace(build_key=published_key)
                    with self.assertRaisesRegex(PgyerError, 'buildKey.*不一致'):
                        upload_artifact(str(artifact), 'fixture', 'notes', platform)
                    self.assertEqual(before, record_path.read_bytes())
        token.assert_not_called(); send.assert_not_called()

    @patch('delivery.pgyer_upload.wait_for_release')
    @patch('delivery.pgyer_upload.upload_file')
    @patch('delivery.pgyer_upload.get_cos_token')
    def test_complete_upload_key_tampering_is_rejected_without_network(self, token, send, query):
        record_path = self.seed_uploaded(self.hap, 'harmonyos', 'key.hap')
        query.return_value = self.release
        self.assertEqual(self.release, upload_artifact(str(self.hap), 'fixture', 'notes', 'harmonyos'))
        original = json.loads(record_path.read_text())
        query.reset_mock()
        for changed in ('other.hap', 'key.apk', 'key.hap.tmp', None, 123):
            with self.subTest(upload_key=changed):
                delivery.write_json_object(record_path, {**original, 'upload_key': changed})
                with self.assertRaisesRegex(PgyerError, '持久分发结果不完整'):
                    upload_artifact(str(self.hap), 'fixture', 'notes', 'harmonyos')
        # 旧 complete 记录没有 upload_key，但仍要求原有 build_key 与 receipt 严格一致。
        original.pop('upload_key')
        delivery.write_json_object(record_path, original)
        self.assertEqual(self.release, upload_artifact(str(self.hap), 'fixture', 'notes', 'harmonyos'))
        token.assert_not_called(); send.assert_not_called(); query.assert_not_called()

    @patch('delivery.pgyer_upload.wait_for_release')
    @patch('delivery.pgyer_upload.upload_file')
    @patch('delivery.pgyer_upload.get_cos_token')
    def test_complete_save_failure_retains_original_storage_key_for_query_resume(self, token, send, query):
        record_path = self.seed_uploaded(self.hap, 'harmonyos', 'key.hap')
        before = record_path.read_bytes()
        query.return_value = self.release
        with patch('delivery.delivery.os.replace', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError):
                upload_artifact(str(self.hap), 'fixture', 'notes', 'harmonyos')
        self.assertEqual(before, record_path.read_bytes())
        self.assertFalse(list(self.state_root.glob('.delivery-*')))
        self.assertEqual(self.release, upload_artifact(str(self.hap), 'fixture', 'notes', 'harmonyos'))
        self.assertEqual(self.release, upload_artifact(str(self.hap), 'fixture', 'notes', 'harmonyos'))
        self.assertEqual(['key.hap', 'key.hap'], [call.kwargs['build_key'] for call in query.call_args_list])
        token.assert_not_called(); send.assert_not_called()

    def test_http_error_hides_reflected_response(self):
        sentinel = "FAKE_REFLECTED_CREDENTIAL"
        body = io.BytesIO(json.dumps({"code": 1, "message": sentinel}).encode())
        error = HTTPError("https://example.test", 500, sentinel, {}, body)
        with patch("delivery.pgyer_upload.urlopen", side_effect=error):
            with self.assertRaises(PgyerError) as caught:
                _request_json(Request("https://example.test"), "fixture", 1)
        self.assertIn("fixture", str(caught.exception))
        self.assertIn("HTTP 500", str(caught.exception))
        self.assertNotIn(sentinel, str(caught.exception))

    def test_http_error_body_is_not_read_and_close_failure_stays_controlled(self):
        sentinel = 'FAKE_REFLECTED_CREDENTIAL'
        for close_error in (None, OSError(sentinel), http.client.IncompleteRead(sentinel.encode(), 128)):
            with self.subTest(close_error=type(close_error).__name__):
                body = MagicMock()
                body.read.side_effect = http.client.IncompleteRead(sentinel.encode(), 128)
                body.close.side_effect = close_error
                error = HTTPError('https://example.test', 500, sentinel, {}, body)
                with patch('delivery.pgyer_upload.urlopen', side_effect=error):
                    with self.assertRaises(PgyerError) as caught:
                        _request_json(Request('https://example.test'), 'fixture', 1)
                self.assertIn('HTTP 500', str(caught.exception))
                self.assertNotIn(sentinel, str(caught.exception))
                body.read.assert_not_called()
                body.close.assert_called_once()

    def test_incomplete_or_io_token_response_keeps_pending_and_never_reposts(self):
        sentinel = 'FAKE_REFLECTED_CREDENTIAL'
        for index, error in enumerate((http.client.IncompleteRead(sentinel.encode(), 128), OSError(sentinel))):
            with self.subTest(error=type(error).__name__), patch.dict(os.environ, {
                    'DELIVERY_STATE_ROOT': str(self.root / ('incomplete-state-' + str(index)))}):
                self.hap.write_bytes(b'hap-' + str(index).encode())
                response = MagicMock()
                response.__enter__.return_value.read.side_effect = error
                with patch('delivery.pgyer_upload.urlopen', return_value=response) as request:
                    with self.assertRaises(PgyerError) as caught:
                        upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos')
                    self.assertNotIn(sentinel, str(caught.exception))
                    self.assertIn(type(error).__name__, str(caught.exception))
                    with self.assertRaisesRegex(PgyerError, '人工核对'):
                        upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos')
                request.assert_called_once()
                self.assertEqual('POST', request.call_args.args[0].get_method())

    def test_incomplete_build_info_response_is_safe_to_query_again(self):
        sentinel = 'FAKE_REFLECTED_CREDENTIAL'
        partial, complete = MagicMock(), MagicMock()
        partial.__enter__.return_value.read.side_effect = http.client.IncompleteRead(sentinel.encode(), 128)
        complete.__enter__.return_value.read.return_value = json.dumps({'code': 0, 'data': {
            'buildKey': 'key', 'buildName': 'fixture', 'buildVersion': '1.0', 'buildVersionNo': '1',
            'buildIdentifier': 'com.example', 'buildShortcutUrl': 'abc'}}).encode()
        with patch('delivery.pgyer_upload.urlopen', side_effect=[partial, complete]) as request:
            with self.assertRaises(PgyerError) as caught:
                wait_for_release('fixture', 'key', timeout=0)
            self.assertNotIn(sentinel, str(caught.exception))
            self.assertEqual(self.release, wait_for_release('fixture', 'key', timeout=0))
        self.assertEqual(['GET', 'GET'], [call.args[0].get_method() for call in request.call_args_list])

    @patch("delivery.pgyer_upload._post_form_json")
    def test_token_error_hides_response_and_invalid_code(self, post):
        sentinel = "FAKE_REFLECTED_CREDENTIAL"
        for code in (1001, sentinel):
            with self.subTest(code=code):
                post.return_value = {"code": code, "message": sentinel}
                with self.assertRaises(PgyerError) as caught:
                    get_cos_token("fixture", "notes", None, None, 1)
                self.assertIn("获取蒲公英上传凭证", str(caught.exception))
                self.assertNotIn(sentinel, str(caught.exception))
                if code == 1001:
                    self.assertIn("1001", str(caught.exception))

    @patch("delivery.pgyer_upload._get_json")
    def test_build_info_error_hides_response_and_invalid_code(self, get):
        sentinel = "FAKE_REFLECTED_CREDENTIAL"
        for code in (1216, 1001, sentinel):
            with self.subTest(code=code):
                get.return_value = {"code": code, "message": sentinel}
                with self.assertRaises(PgyerError) as caught:
                    wait_for_release("fixture", "build", timeout=0)
                self.assertNotIn(sentinel, str(caught.exception))
                if isinstance(code, int):
                    self.assertIn(str(code), str(caught.exception))

    @patch('delivery.pgyer_upload._post_form_json')
    def test_harmony_token_and_hap_upload_use_official_build_type(self, post):
        post.return_value = {'code': 0, 'data': {'endpoint': 'https://upload.example.test', 'key': 'key',
                             'params': {'key': 'key', 'signature': 'signature', 'x-cos-security-token': 'token'}}}
        get_cos_token('api', 'QA', None, None, 1, build_type='harmonyos')
        self.assertEqual('harmonyos', post.call_args.args[1]['buildType'])
        with TemporaryDirectory() as directory, patch('delivery.pgyer_upload.upload_file') as send, \
                patch('delivery.pgyer_upload.wait_for_release', return_value=self.release):
            hap = Path(directory) / 'signed.hap'; hap.write_bytes(b'hap')
            self.assertEqual(self.release, upload_artifact(str(hap), 'api', 'QA', platform='harmonyos'))
            send.assert_called_once()

    @patch('delivery.pgyer_upload._post_multipart')
    def test_harmony_p12_uses_fixed_endpoint_and_hides_failure_response(self, send):
        with TemporaryDirectory() as directory:
            p12 = Path(directory) / 'release.p12'; p12.write_bytes(b'private fixture')
            send.return_value = (200, b'{"code":0}')
            upload_harmony_certificate('api', 'build', p12, 'secret')
            self.assertEqual('https://upload.pgyer.com/apiv2/app/uploadHarmonyCert', send.call_args.args[0])
            self.assertEqual({'_api_key': 'api', 'buildKey': 'build', 'password': 'secret'}, send.call_args.args[1])
            for index, (status, body) in enumerate([(200, b'{"code":1,"message":"secret private fixture"}'), (500, b'secret')]):
                send.return_value = (status, body)
                with self.assertRaises(PgyerError) as error:
                    upload_harmony_certificate('api', 'failed-' + str(index), p12, 'secret')
                self.assertNotIn('secret', str(error.exception))
                self.assertNotIn('private fixture', str(error.exception))

    @patch("delivery.pgyer_upload._post_form_json")
    def test_ios_token_request_uses_ios_build_type(self, post_form_json):
        post_form_json.return_value = {
            "code": 0,
            "data": {
                "endpoint": "https://upload.example.test/path",
                "key": "build-key",
                "params": {
                    "key": "build-key",
                    "signature": "signature",
                    "x-cos-security-token": "token",
                },
            },
        }

        get_cos_token("api", "notes", None, None, 1, build_type="ios")

        fields = post_form_json.call_args[0][1]
        self.assertEqual("ios", fields["buildType"])

    def test_upload_artifact_rejects_wrong_platform_extension(self):
        with TemporaryDirectory() as directory:
            ipa = Path(directory) / "app.ipa"
            ipa.write_bytes(b"ipa")

            with self.assertRaises(PgyerError):
                upload_artifact(str(ipa), "api", "notes", platform="android")

    def test_upload_file_rejects_http_endpoint_before_reading_artifact(self):
        with TemporaryDirectory() as directory:
            apk = Path(directory) / "app.apk"
            apk.write_bytes(b"apk")

            with self.assertRaisesRegex(PgyerError, "HTTPS"):
                upload_file(
                    "http://upload.example.test/path",
                    {
                        "key": "key",
                        "signature": "signature",
                        "x-cos-security-token": "token",
                    },
                    apk,
                    timeout=1,
                )

    def test_maps_build_info_without_using_build_key_as_download_url(self):
        release = release_info_from_api(
            {
                "buildKey": "build-key",
                "buildName": "妙手中医",
                "buildVersion": "3.2.1",
                "buildVersionNo": "321",
                "buildIdentifier": "com.example.app",
                "buildShortcutUrl": "abcd",
                "buildQRCodeURL": "https://example.test/qrcode",
            }
        )

        self.assertEqual(
            ArtifactInfo(
                name="妙手中医",
                version="3.2.1",
                version_code="321",
                package_name="com.example.app",
                build_key="build-key",
                download_url="https://www.pgyer.com/abcd",
                qr_code_url="https://example.test/qrcode",
            ),
            release,
        )

    def test_rejects_incomplete_release_result(self):
        with self.assertRaises(PgyerError):
            release_info_from_api({"buildKey": "only-key"})

    @patch("delivery.pgyer_upload.time.sleep")
    @patch("delivery.pgyer_upload._get_json")
    def test_build_info_polling_survives_transient_network_error(self, get_json, _sleep):
        get_json.side_effect = [
            PgyerError("temporary"),
            {"code": 1247, "message": "publishing"},
            {
                "code": 0,
                "data": {
                    "buildKey": "key",
                    "buildName": "App",
                    "buildVersion": "1.0",
                    "buildVersionNo": "1",
                    "buildIdentifier": "com.example",
                    "buildShortcutUrl": "abcd",
                    "buildQRCodeURL": "https://example.test/qr",
                },
            },
        ]

        release = wait_for_release("api-key", "key", timeout=1, poll_interval=0)

        self.assertEqual("key", release.build_key)
        self.assertEqual(3, get_json.call_count)

    @patch('delivery.pgyer_upload.wait_for_release')
    @patch('delivery.pgyer_upload.upload_file')
    @patch('delivery.pgyer_upload.get_cos_token', return_value=('https://example.test', 'key', {'signature': 'SECRET_SIGNATURE'}))
    def test_unknown_cos_response_recovers_by_query_and_complete_is_idempotent(self, token, send, query):
        send.side_effect = PgyerError('response unknown')
        with self.assertRaises(PgyerError):
            upload_artifact(str(self.hap), 'SECRET_API_KEY', 'SECRET_NOTES', platform='harmonyos', install_password='SECRET_PASSWORD')
        record = json.loads(next(self.state_root.glob('*.json')).read_text())
        self.assertEqual(('upload-pending', 'key'), (record['phase'], record['build_key']))
        query.return_value = self.release
        self.assertEqual(self.release, upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos'))
        self.assertEqual(self.release, upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos'))
        token.assert_called_once(); send.assert_called_once(); query.assert_called_once()
        self.assertEqual('key', query.call_args.kwargs['build_key'])
        for value in ('SECRET_API_KEY', 'SECRET_NOTES', 'SECRET_PASSWORD', 'SECRET_SIGNATURE', 'https://example.test'):
            self.assertNotIn(value, next(self.state_root.glob('*.json')).read_text())

    @patch('delivery.pgyer_upload.wait_for_release')
    @patch('delivery.pgyer_upload.upload_file')
    @patch('delivery.pgyer_upload.get_cos_token')
    def test_token_response_unknown_blocks_new_post_without_key(self, token, send, query):
        token.side_effect = PgyerError('response unknown')
        with self.assertRaises(PgyerError):
            upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos')
        with self.assertRaisesRegex(PgyerError, '人工核对'):
            upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos')
        token.assert_called_once(); send.assert_not_called(); query.assert_not_called()
        record = json.loads(next(self.state_root.glob('*.json')).read_text())
        self.assertEqual('pending', record['phase'])
        self.assertNotIn('build_key', record)

    @patch('delivery.pgyer_upload.wait_for_release')
    @patch('delivery.pgyer_upload.upload_file')
    @patch('delivery.pgyer_upload.get_cos_token')
    def test_crash_after_token_preserves_key_and_blocks_post(self, token, send, query):
        identity = {'operation': 'pgyer-upload', 'platform': 'harmonyos', 'brand': '', 'artifact_sha256': artifact_sha256(self.hap), 'channel_shortcut': ''}
        with delivery_operation(identity, scope={key: identity[key] for key in ('operation', 'platform', 'brand')}) as (_state, save):
            save(phase='token', build_key='key')
        with self.assertRaisesRegex(PgyerError, '人工核对'):
            upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos')
        token.assert_not_called(); send.assert_not_called(); query.assert_not_called()
        self.assertEqual('key', json.loads(next(self.state_root.glob('*.json')).read_text())['build_key'])

    @patch('delivery.pgyer_upload.wait_for_release')
    @patch('delivery.pgyer_upload.upload_file')
    @patch('delivery.pgyer_upload.get_cos_token', return_value=('https://example.test', 'key', {}))
    def test_publish_timeout_preserves_uploaded_key_for_query_only_recovery(self, token, send, query):
        query.side_effect = [PgyerError('publishing timeout'), self.release]
        with self.assertRaises(PgyerError):
            upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos')
        self.assertEqual('uploaded', json.loads(next(self.state_root.glob('*.json')).read_text())['phase'])
        self.assertEqual(self.release, upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos'))
        token.assert_called_once(); send.assert_called_once()
        self.assertEqual(['key', 'key'], [call.kwargs['build_key'] for call in query.call_args_list])

    @patch('delivery.pgyer_upload.wait_for_release')
    @patch('delivery.pgyer_upload.upload_file')
    @patch('delivery.pgyer_upload.get_cos_token')
    def test_distinct_ref_artifact_or_channel_does_not_reuse_receipt(self, token, send, query):
        token.return_value = ('https://example.test', 'key', {})
        query.return_value = self.release
        upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos', delivery_identity={'branch': 'dev/cihang'})
        for index, (branch, channel, content) in enumerate([
                ('dev/jimin', None, b'hap fixture'), ('dev/cihang', 'new-channel', b'hap fixture'),
                ('dev/cihang', None, b'new artifact')]):
            token.return_value = ('https://example.test', 'key-' + str(index), {})
            query.return_value = self.release._replace(build_key='key-' + str(index))
            self.hap.write_bytes(content)
            result = upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos', channel_shortcut=channel, delivery_identity={'branch': branch})
            self.assertEqual('key-' + str(index), result.build_key)
        self.assertEqual(4, token.call_count)
        self.assertEqual(4, send.call_count)

    @patch('delivery.pgyer_upload.wait_for_release')
    @patch('delivery.pgyer_upload.upload_file')
    @patch('delivery.pgyer_upload.get_cos_token')
    def test_changed_artifact_is_never_confirmed_as_original_upload(self, token, send, query):
        for during_upload in (False, True):
            with self.subTest(during_upload=during_upload), patch.dict(os.environ, {
                    'DELIVERY_STATE_ROOT': str(self.root / ('mutation-state-' + str(during_upload)))}):
                self.hap.write_bytes(b'original-' + str(during_upload).encode())
                token.reset_mock(); send.reset_mock(); query.reset_mock()
                def mutate(*args):
                    self.hap.write_bytes(b'changed')
                    return ('https://example.test', 'key', {})
                token.side_effect = None if during_upload else mutate
                token.return_value = ('https://example.test', 'key', {})
                send.side_effect = mutate if during_upload else None
                with self.assertRaisesRegex(PgyerError, '改变'):
                    upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos')
                query.assert_not_called()
                self.assertEqual(int(during_upload), send.call_count)

    @patch('delivery.pgyer_upload.wait_for_release')
    @patch('delivery.pgyer_upload.upload_file', side_effect=PgyerError('response unknown'))
    @patch('delivery.pgyer_upload.get_cos_token', return_value=('https://example.test', 'key', {}))
    def test_unknown_upload_blocks_changed_artifact_ref_environment_engine_or_channel(self, token, send, query):
        identity = {'brand': 'fixture', 'branch': 'dev/fixture', 'commit': 'abc123',
                    'environment': 'dev', 'ui_engine': 'cmp'}
        with self.assertRaises(PgyerError):
            upload_artifact(str(self.hap), 'fixture', 'notes', 'harmonyos', delivery_identity=identity)
        for field, value in [('branch', 'dev/other'), ('commit', 'def456'), ('environment', 'prod'),
                             ('ui_engine', 'kuikly'), ('channel', 'other'), ('artifact', b'rebuilt')]:
            with self.subTest(field=field):
                changed_identity = {**identity, **({field: value} if field in identity else {})}
                self.hap.write_bytes(value if field == 'artifact' else b'hap fixture')
                with self.assertRaises(DeliveryContextError):
                    upload_artifact(str(self.hap), 'fixture', 'notes', 'harmonyos',
                                    delivery_identity=changed_identity,
                                    channel_shortcut=value if field == 'channel' else None)
        token.assert_called_once(); send.assert_called_once(); query.assert_not_called()

    @patch('delivery.pgyer_upload.wait_for_release')
    @patch('delivery.pgyer_upload.upload_file', side_effect=PgyerError('response unknown'))
    @patch('delivery.pgyer_upload.get_cos_token', return_value=('https://example.test', 'key', {}))
    def test_real_ios_caller_cannot_repost_rebuilt_ipa_after_unknown_response(self, token, send, query):
        from delivery.ios_release import upload_test_release
        from tests.delivery.test_ios_release import IosReleaseTest

        with contextlib.redirect_stdout(io.StringIO()):
            config, ipa, output = IosReleaseTest()._prepare(self.root)
        args = argparse.Namespace(config=config, context=output / 'context.json',
                                  output=output / 'release-info.json', dry_run=False)
        before = artifact_sha256(ipa)
        with patch.dict(os.environ, {'PGYER_API_KEY': 'fixture'}):
            with self.assertRaises(PgyerError):
                upload_test_release(args)
            with zipfile.ZipFile(ipa) as archive:
                entries = {name: archive.read(name) for name in archive.namelist()}
            for name, content in entries.items():
                if name.endswith('.app/Info.plist'):
                    metadata = plistlib.loads(content)
                    metadata['CFBundleVersion'] = '20261011220000'
                    entries[name] = plistlib.dumps(metadata)
            with zipfile.ZipFile(ipa, 'w') as archive:
                for name, content in entries.items():
                    archive.writestr(name, content)
            context = json.loads(args.context.read_text())
            original_ref = (context['branch'], context['commit'])
            context['version_code'] = '20261011220000'
            args.context.write_text(json.dumps(context))
            self.assertNotEqual(before, artifact_sha256(ipa))
            with self.assertRaises(DeliveryContextError):
                upload_test_release(args)
            self.assertEqual(original_ref, (context['branch'], context['commit']))
        token.assert_called_once(); send.assert_called_once(); query.assert_not_called()

    @patch('delivery.pgyer_upload.wait_for_release')
    @patch('delivery.pgyer_upload.upload_file')
    @patch('delivery.pgyer_upload.get_cos_token', return_value=('https://example.test', 'key', {}))
    def test_artifact_save_failure_at_every_phase_never_repeats_remote_write(self, token, send, query):
        write = delivery.write_json_object
        query.return_value = self.release
        for phase in ('pending', 'token', 'upload-pending', 'uploaded', 'complete'):
            with self.subTest(phase=phase), patch.dict(os.environ, {
                    'DELIVERY_STATE_ROOT': str(self.root / ('save-' + phase))}):
                token.reset_mock(); send.reset_mock(); query.reset_mock()
                def fail_save(path, value):
                    if value.get('phase') == phase:
                        raise OSError('fixture disk failure')
                    write(path, value)
                with patch.object(delivery, 'write_json_object', side_effect=fail_save):
                    with self.assertRaises(OSError):
                        upload_artifact(str(self.hap), 'fixture', 'notes', 'harmonyos')
                if phase in ('token', 'upload-pending'):
                    with self.assertRaisesRegex(PgyerError, '人工核对'):
                        upload_artifact(str(self.hap), 'fixture', 'notes', 'harmonyos')
                else:
                    self.assertEqual(self.release, upload_artifact(str(self.hap), 'fixture', 'notes', 'harmonyos'))
                self.assertEqual(1, token.call_count)
                self.assertEqual(0 if phase in ('token', 'upload-pending') else 1, send.call_count)
                self.assertEqual(2 if phase == 'complete' else int(phase not in ('token', 'upload-pending')), query.call_count)

    @patch('delivery.pgyer_upload._post_multipart')
    def test_certificate_save_failure_at_every_phase_never_repeats_remote_write(self, send):
        write = delivery.write_json_object
        for phase in ('pending', 'rejected', 'completed'):
            with self.subTest(phase=phase), patch.dict(os.environ, {
                    'DELIVERY_STATE_ROOT': str(self.root / ('certificate-save-' + phase))}):
                send.reset_mock()
                send.return_value = (200, b'{"code":1}' if phase == 'rejected' else b'{"code":0}')
                def fail_save(path, value):
                    if value.get('phase') == phase:
                        raise OSError('fixture disk failure')
                    write(path, value)
                with patch.object(delivery, 'write_json_object', side_effect=fail_save):
                    with self.assertRaises(OSError):
                        upload_harmony_certificate('fixture', 'key', self.p12, 'fixture')
                if phase == 'pending':
                    upload_harmony_certificate('fixture', 'key', self.p12, 'fixture')
                else:
                    with self.assertRaisesRegex(PgyerError, '人工核对'):
                        upload_harmony_certificate('fixture', 'key', self.p12, 'fixture')
                send.assert_called_once()

    def test_unapproved_identity_fields_cannot_store_secret(self):
        with self.assertRaisesRegex(PgyerError, '不支持的字段'):
            upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos', delivery_identity={'password': 'SECRET_PASSWORD'})
        self.assertFalse(self.state_root.exists())

    @patch('delivery.pgyer_upload.delivery_operation')
    @patch('delivery.pgyer_upload.get_cos_token')
    @patch('delivery.pgyer_upload.upload_file')
    @patch('delivery.pgyer_upload.wait_for_release')
    @patch('delivery.pgyer_upload._post_multipart')
    def test_null_or_missing_phase_with_other_state_never_starts_post(self, certificate, query, send, token, operation):
        for fields in ({'phase': None}, {'build_key': 'key'}, {'status': 'sent'}):
            with self.subTest(fields=fields):
                operation.return_value.__enter__.return_value = ({'version': 1, 'identity': {}, **fields}, lambda **kw: None)
                with self.assertRaisesRegex(PgyerError, '状态无效'):
                    upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos')
                with self.assertRaisesRegex(PgyerError, '状态无效'):
                    upload_harmony_certificate('fixture', 'key', self.p12, 'fixture')
        token.assert_not_called(); send.assert_not_called(); query.assert_not_called(); certificate.assert_not_called()

    @patch('delivery.pgyer_upload.get_cos_token')
    @patch('delivery.pgyer_upload.upload_file')
    @patch('delivery.pgyer_upload.wait_for_release')
    def test_complete_receipt_cannot_change_original_build_key(self, query, send, token):
        identity = {'operation': 'pgyer-upload', 'platform': 'harmonyos', 'brand': '', 'artifact_sha256': artifact_sha256(self.hap), 'channel_shortcut': ''}
        with delivery_operation(identity, scope={key: identity[key] for key in ('operation', 'platform', 'brand')}) as (_state, save):
            save(phase='complete', build_key='original', receipt=self.release.to_dict())
        with self.assertRaisesRegex(PgyerError, '持久分发结果不完整'):
            upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos')
        token.assert_not_called(); send.assert_not_called(); query.assert_not_called()

    @patch('delivery.pgyer_upload.wait_for_release')
    @patch('delivery.pgyer_upload.upload_file')
    @patch('delivery.pgyer_upload.get_cos_token', return_value=('https://example.test', 'key', {}))
    def test_query_cannot_replace_original_key_and_recovery_keeps_it(self, token, send, query):
        query.return_value = self.release._replace(build_key='wrong-key')
        with self.assertRaisesRegex(PgyerError, 'buildKey.*不一致'):
            upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos')
        record = json.loads(next(self.state_root.glob('*.json')).read_text())
        self.assertEqual(('uploaded', 'key'), (record['phase'], record['build_key']))
        self.assertNotIn('receipt', record)
        query.return_value = self.release
        self.assertEqual(self.release, upload_artifact(str(self.hap), 'fixture', 'notes', platform='harmonyos'))
        token.assert_called_once(); send.assert_called_once()
        self.assertEqual(['key', 'key'], [call.kwargs['build_key'] for call in query.call_args_list])

    def test_release_info_write_failure_preserves_previous_report(self):
        path = self.root / 'release-info.json'
        write_release_info(self.release, path)
        before = path.read_bytes()
        with patch('delivery.delivery.os.replace', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError):
                write_release_info(self.release._replace(build_key='new-key'), path)
        self.assertEqual(before, path.read_bytes())
        self.assertFalse(list(self.root.glob('.delivery-*')))

    @patch('delivery.pgyer_upload._post_multipart')
    def test_certificate_unknown_blocks_retry_and_replacement_material(self, send):
        send.side_effect = PgyerError('response unknown')
        with self.assertRaises(PgyerError):
            upload_harmony_certificate('fixture', 'key', self.p12, 'SECRET_PASSWORD')
        with self.assertRaisesRegex(PgyerError, '人工核对'):
            upload_harmony_certificate('fixture', 'key', self.p12, 'SECRET_PASSWORD')
        self.p12.write_bytes(b'replacement')
        with self.assertRaisesRegex(PgyerError, '不一致'):
            upload_harmony_certificate('fixture', 'key', self.p12, 'SECRET_PASSWORD')
        send.assert_called_once()
        self.assertNotIn('SECRET_PASSWORD', next(self.state_root.glob('*.json')).read_text())

    @patch('delivery.pgyer_upload._post_multipart')
    def test_certificate_explicit_rejection_can_retry_then_complete_is_idempotent(self, send):
        send.side_effect = [(200, b'{"code":1}'), (200, b'{"code":0}')]
        with self.assertRaises(PgyerError):
            upload_harmony_certificate('fixture', 'key', self.p12, 'fixture')
        upload_harmony_certificate('fixture', 'key', self.p12, 'fixture')
        upload_harmony_certificate('fixture', 'key', self.p12, 'fixture')
        self.assertEqual(2, send.call_count)

    @patch('delivery.pgyer_upload._post_multipart')
    def test_certificate_invalid_or_http_error_is_unknown_not_rejected(self, send):
        for index, response in enumerate([(500, b'bad'), (200, b'not json'), (200, b'{}'), (200, b'{"code":false}'), (200, b'{"code":0.1}')]):
            with self.subTest(response=response):
                send.return_value = response
                with self.assertRaises(PgyerError):
                    upload_harmony_certificate('fixture', str(index), self.p12, 'fixture')
                with self.assertRaisesRegex(PgyerError, '人工核对'):
                    upload_harmony_certificate('fixture', str(index), self.p12, 'fixture')
        self.assertEqual(5, send.call_count)

    @patch('delivery.pgyer_upload._post_multipart')
    def test_certificate_mutation_during_request_stays_unknown(self, send):
        def mutate(*args):
            self.p12.write_bytes(b'changed')
            return 200, b'{"code":0}'
        send.side_effect = mutate
        with self.assertRaisesRegex(PgyerError, '改变'):
            upload_harmony_certificate('fixture', 'key', self.p12, 'fixture')
        self.assertEqual('pending', json.loads(next(self.state_root.glob('*.json')).read_text())['phase'])

    def test_two_processes_recover_only_original_material_under_same_scope(self):
        context = multiprocessing.get_context('spawn')
        rebuilt = self.root / 'rebuilt.hap'
        rebuilt.write_bytes(b'different signed hap')
        for phase in ('upload-pending', 'uploaded'):
            with self.subTest(phase=phase):
                state_root = self.root / ('concurrent-material-' + phase)
                identity = {'operation': 'pgyer-upload', 'platform': 'harmonyos', 'brand': '',
                            'artifact_sha256': artifact_sha256(self.hap), 'channel_shortcut': ''}
                start = context.Barrier(2)
                posts = context.Value('i', 0)
                queries = context.Value('i', 0)
                tokens = context.Value('i', 0)
                result = context.Queue()
                lock_attempts = context.Queue()
                self.addCleanup(result.close)
                self.addCleanup(lock_attempts.close)
                processes = [context.Process(target=concurrent_upload, args=(
                    str(artifact), str(state_root), start, posts, result, False, queries, tokens, lock_attempts))
                    for artifact in (self.hap, rebuilt)]
                with patch.dict(os.environ, {'DELIVERY_STATE_ROOT': str(state_root)}):
                    with delivery_operation(identity, scope={key: identity[key] for key in ('operation', 'platform', 'brand')}) as (_state, save):
                        save(phase=phase, build_key='key')
                        for process in processes:
                            process.start()
                            self.addCleanup(lambda process=process: process.terminate() if process.is_alive() else None)
                        # 两个调用均在旧结果未知时尝试加锁，排除完成后有意发起的新交付。
                        self.assertEqual(['locking', 'locking'], [lock_attempts.get(timeout=10), lock_attempts.get(timeout=10)])
                for process in processes:
                    process.join(timeout=10)
                    self.assertEqual(0, process.exitcode)
                self.assertEqual(['DeliveryContextError', 'ok'], sorted([result.get(timeout=1), result.get(timeout=1)]))
                self.assertEqual(0, posts.value)
                self.assertEqual(0, tokens.value)
                self.assertEqual(1, queries.value)

    def test_two_processes_upload_once_for_same_operation(self):
        context = multiprocessing.get_context('spawn')
        for certificate, phase in ((False, None), (True, None), (False, 'upload-pending'),
                                   (False, 'uploaded'), (True, 'pending'), (True, 'completed')):
            with self.subTest(certificate=certificate, phase=phase):
                state_root = self.root / ('concurrent-{}-{}'.format(certificate, phase))
                if phase is not None:
                    with patch.dict(os.environ, {'DELIVERY_STATE_ROOT': str(state_root)}):
                        if certificate:
                            with delivery_operation({'operation': 'pgyer-certificate', 'build_key': 'key'}) as (_state, save):
                                save(phase=phase, certificate_sha256=artifact_sha256(self.p12))
                        else:
                            identity = {'operation': 'pgyer-upload', 'platform': 'harmonyos', 'brand': '',
                                        'artifact_sha256': artifact_sha256(self.hap), 'channel_shortcut': ''}
                            with delivery_operation(identity, scope={key: identity[key] for key in ('operation', 'platform', 'brand')}) as (_state, save):
                                save(phase=phase, build_key='key')
                start = context.Barrier(2)
                posts = context.Value('i', 0)
                result = context.Queue()
                self.addCleanup(result.close)
                artifact = self.p12 if certificate else self.hap
                processes = [context.Process(target=concurrent_upload, args=(str(artifact), str(state_root), start, posts, result, certificate)) for _ in range(2)]
                for process in processes:
                    process.start()
                    self.addCleanup(lambda process=process: process.terminate() if process.is_alive() else None)
                for process in processes:
                    process.join(timeout=10)
                    self.assertEqual(0, process.exitcode)
                expected = 'PgyerError' if certificate and phase == 'pending' else 'ok'
                self.assertEqual([expected, expected], [result.get(timeout=1), result.get(timeout=1)])
                self.assertEqual(int(phase is None), posts.value)


if __name__ == "__main__":
    unittest.main()
