import json
import os
from pathlib import Path
import shutil
import tempfile
import subprocess
import unittest
import warnings
from unittest.mock import patch
import zipfile

from delivery import harmony_release as release


class HarmonyReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        environment = patch.dict(os.environ, {'DELIVERY_STATE_ROOT': str(self.root / 'state'),
                                               'PGYER_API_KEY': 'fixture', 'HARMONY_P12_PASSWORD': 'fixture'})
        environment.start()
        self.addCleanup(environment.stop)
        self.config = {'brand': 'cihang', 'bundleName': 'com.vessel.mercy.tcm.app', 'teamKey': 'cihang',
                       'enabled': True, 'credentialNamespace': 'harmony-team-cihang', **release.build_settings('Release')}
        self.hap = self.root / 'unsigned.hap'
        self.write_hap()
        (self.root / 'release.p12').write_bytes(b'fixture certificate')

    def write_hap(self, bundle=None, duplicate=False, build_type='Release', debug=None, metadata=None, mode=None):
        settings = release.build_settings(build_type)
        module = {'app': {'bundleName': bundle or self.config['bundleName'], 'versionName': '1.0.0', 'versionCode': 1,
                          'debug': settings['buildMode'] == 'debug' if debug is None else debug, 'buildMode': mode or settings['buildMode']},
                  'module': {'type': 'entry', 'name': 'entry'}}
        with zipfile.ZipFile(self.hap, 'w') as archive:
            archive.writestr('module.json', json.dumps(module))
            archive.writestr('resources/resfile/ci-build.json', json.dumps(settings if metadata is None else metadata))
            if duplicate:
                archive.writestr('extra.hap', 'unexpected')

    def test_plan_accepts_only_boolean_notify(self):
        with patch.object(release.provisioning, 'load_brand', return_value=self.config):
            self.assertTrue(release.plan(self.root / 'config', 'cihang', 'dev/cihang', False, True)['notify'])
            self.assertFalse(release.plan(self.root / 'config', 'cihang', 'dev/cihang', True, False)['notify'])
            for value in (None, 'true', 1):
                with self.assertRaisesRegex(release.HarmonyReleaseError, 'Boolean'):
                    release.plan(self.root / 'config', 'cihang', 'dev/cihang', True, value)

    def test_notify_false_and_dry_run_do_not_read_reports_or_credentials(self):
        with patch.object(release, 'read_json') as read, patch.object(release.dingtalk_notify, 'load_brand_config') as brand, patch.object(release.dingtalk_notify, 'send_release_notification') as send:
            for notify, dry in ((False, False), (False, True), (True, True)):
                result = release.notify(self.hap, self.root / 'absent', {**self.config, 'notify': notify, 'dryRun': dry}, self.root / 'absent')
                self.assertFalse(result['notified'])
            read.assert_not_called(); brand.assert_not_called(); send.assert_not_called()

    def test_upload_reverifies_and_requires_certificate_success_before_distribution(self):
        from delivery.delivery import ArtifactInfo
        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps({'cihang': {'displayName': '慈航中医'}}))
        output = self.root / 'output'
        report = {'brand': 'cihang', 'bundleName': self.config['bundleName'], 'versionName': '1.0.0', 'versionCode': 1,
                  'sha256': release.hashlib.sha256(self.hap.read_bytes()).hexdigest(), 'distributed': False}
        published = ArtifactInfo('慈航中医', '1.0.0', '1', self.config['bundleName'], 'build', 'https://www.pgyer.com/abc', '')
        config = {**self.config, 'dryRun': False, 'branch': 'dev/cihang', 'tag': '', 'commit': 'a' * 40}
        with patch.object(release, 'materials', return_value={'p12': self.root / 'release.p12'}), \
                patch.object(release, 'tools', return_value={}), patch.object(release, 'verify', side_effect=lambda *a: dict(report)) as verify, \
                patch.object(release.pgyer_upload, 'upload_artifact', return_value=published) as send, \
                patch.object(release.pgyer_upload, 'upload_harmony_certificate') as cert, \
                patch.dict(os.environ, {'PGYER_API_KEY': 'api', 'HARMONY_P12_PASSWORD': 'password'}):
            dry = release.upload(self.hap, output, {**config, 'dryRun': True}, config_path)
            self.assertFalse(dry['distributed']); verify.assert_called_once(); send.assert_not_called(); cert.assert_not_called()
            result = release.upload(self.hap, output, config, config_path)
            self.assertTrue(result['distributed']); cert.assert_called_once_with('api', 'build', self.root / 'release.p12', 'password')
            signature = json.loads((output / 'signature-report.json').read_text())
            self.assertTrue(signature['distributed'])
            self.assertEqual(('a' * 40, 'dev/cihang', '', 'kuikly'),
                             tuple(signature[key] for key in ('commit', 'branch', 'tag', 'uiEngine')))
            self.assertEqual(('a' * 40, 'dev/cihang', ''), tuple(result[key] for key in ('commit', 'branch', 'tag')))
            cert.reset_mock(); send.return_value = published._replace(package_name='wrong')
            with self.assertRaisesRegex(release.HarmonyReleaseError, '未上传 P12'):
                release.upload(self.hap, output, config, config_path)
            cert.assert_not_called()
            self.assertFalse(json.loads((output / 'release-info.json').read_text())['distributed'])
            send.return_value = published; cert.side_effect = release.pgyer_upload.PgyerError('certificate rejected')
            with self.assertRaises(release.pgyer_upload.PgyerError):
                release.upload(self.hap, output, config, config_path)
            self.assertFalse(json.loads((output / 'signature-report.json').read_text())['distributed'])
            self.assertFalse(json.loads((output / 'release-info.json').read_text())['distributed'])
            self.assertEqual('build', json.loads((output / 'release-info.json').read_text())['build_key'])

    def test_upload_signature_failure_never_calls_pgyer(self):
        output = self.root / 'output'
        output.mkdir()
        for name in ('signature-report.json', 'release-info.json'):
            (output / name).write_text('{"distributed":true}')
        with patch.object(release, 'materials', return_value={}), patch.object(release, 'tools', return_value={}), \
                patch.object(release, 'verify', side_effect=release.HarmonyReleaseError('bad signature')), \
                patch.object(release.pgyer_upload, 'upload_artifact') as send, \
                patch.object(release.pgyer_upload, 'upload_harmony_certificate') as cert:
            with self.assertRaisesRegex(release.HarmonyReleaseError, 'bad signature'):
                release.upload(self.hap, output, {**self.config, 'dryRun': False}, self.root / 'absent')
            send.assert_not_called(); cert.assert_not_called()
            self.assertEqual('{"distributed":true}', (output / 'signature-report.json').read_text())
            self.assertEqual('{"distributed":true}', (output / 'release-info.json').read_text())

    def test_upload_unknown_results_survive_workspace_cleanup_without_repeating_posts(self):
        config = {**self.config, 'dryRun': False, 'branch': 'dev/cihang', 'tag': '', 'commit': 'a' * 40}
        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps({'cihang': {'displayName': '慈航中医'}}))
        p12 = self.root / 'release.p12'
        p12.write_bytes(b'fixture certificate')
        report = {**release.hap_metadata(self.hap, config), 'brand': 'cihang',
                  'sha256': release.hashlib.sha256(self.hap.read_bytes()).hexdigest(), 'distributed': False}
        published = {'code': 0, 'data': {'buildName': '慈航中医', 'buildVersion': '1.0.0', 'buildVersionNo': '1',
                     'buildIdentifier': config['bundleName'], 'buildKey': 'build', 'buildShortcutUrl': 'abc'}}
        for unknown_stage in ('hap', 'certificate'):
            with self.subTest(unknown_stage=unknown_stage):
                output = self.root / unknown_stage
                shutil.rmtree(self.root / 'state', ignore_errors=True)
                requests = []

                def post(endpoint, *_args):
                    requests.append(endpoint)
                    if len(requests) == 1 and unknown_stage == 'hap' or len(requests) == 2 and unknown_stage == 'certificate':
                        raise release.pgyer_upload.PgyerError('unknown response')
                    return (200, b'{"code":0}') if endpoint == release.pgyer_upload.HARMONY_CERT_URL else (204, b'')

                with patch.object(release, 'materials', return_value={'p12': p12}), \
                        patch.object(release, 'tools', return_value={}), \
                        patch.object(release, 'verify', side_effect=lambda *args: {**report,
                            'sha256': release.hashlib.sha256(self.hap.read_bytes()).hexdigest()}) as verify, \
                        patch.object(release.pgyer_upload, 'get_cos_token', return_value=('https://fixture.test', 'build', {})) as token, \
                        patch.object(release.pgyer_upload, '_post_multipart', side_effect=post), \
                        patch.object(release.pgyer_upload, '_get_json', return_value=published) as query:
                    with self.assertRaisesRegex(release.pgyer_upload.PgyerError, 'unknown response'):
                        release.upload(self.hap, output, config, config_path)
                    original = self.hap.read_bytes()
                    self.hap.write_bytes(original + b'changed signature')
                    with self.assertRaisesRegex(release.DeliveryContextError, '禁止重新上传或发送'):
                        release.upload(self.hap, output, config, config_path)
                    self.hap.write_bytes(original)
                    shutil.rmtree(output)
                    if unknown_stage == 'hap':
                        self.assertTrue(release.upload(self.hap, output, config, config_path)['distributed'])
                        self.assertTrue(release.upload(self.hap, output, config, config_path)['distributed'])
                    else:
                        with self.assertRaisesRegex(release.pgyer_upload.PgyerError, '结果未知'):
                            release.upload(self.hap, output, config, config_path)
                        self.assertFalse(json.loads((output / 'release-info.json').read_text())['distributed'])
                    self.assertEqual(['https://fixture.test', release.pgyer_upload.HARMONY_CERT_URL], requests)
                    token.assert_called_once()
                    query.assert_called_once()
                    self.assertGreaterEqual(verify.call_count, 2)

    def test_upload_dry_run_preserves_existing_real_receipts_and_does_not_open_state(self):
        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps({'cihang': {'displayName': '慈航中医'}}))
        output = self.root / 'output'
        output.mkdir()
        for name in ('signature-report.json', 'release-info.json'):
            (output / name).write_text('{"distributed":true}')
        report = {**release.hap_metadata(self.hap, self.config), 'sha256': 'a' * 64}
        with patch.object(release, 'materials', return_value={}), patch.object(release, 'tools', return_value={}), \
                patch.object(release, 'verify', return_value=report), \
                patch.object(release.pgyer_upload, 'delivery_operation') as state, \
                patch.dict(os.environ, {'DELIVERY_STATE_ROOT': ''}):
            result = release.upload(self.hap, output, {**self.config, 'dryRun': True, 'branch': 'dev/cihang', 'tag': ''}, config_path)
        self.assertFalse(result['distributed'])
        state.assert_not_called()
        for name in ('signature-report.json', 'release-info.json'):
            self.assertEqual('{"distributed":true}', (output / name).read_text())

    def test_distribution_receipt_write_failure_blocks_new_hap_and_recovers_without_posts(self):
        from delivery.delivery import write_json_object
        config = {**self.config, 'dryRun': False, 'branch': 'dev/cihang', 'tag': '', 'commit': 'a' * 40}
        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps({'cihang': {'displayName': '慈航中医'}}))
        output = self.root / 'output'
        report = {**release.hap_metadata(self.hap, config), 'brand': 'cihang',
                  'sha256': release.hashlib.sha256(self.hap.read_bytes()).hexdigest(), 'distributed': False}
        published = {'code': 0, 'data': {'buildName': '慈航中医', 'buildVersion': '1.0.0', 'buildVersionNo': '1',
                     'buildIdentifier': config['bundleName'], 'buildKey': 'build', 'buildShortcutUrl': 'abc'}}
        def post(endpoint, *_args):
            return (200, b'{"code":0}') if endpoint == release.pgyer_upload.HARMONY_CERT_URL else (204, b'')
        def fail_final_receipt(path, value):
            if value['identity']['operation'] == 'harmony-distribution' and value['phase'] == 'complete':
                raise OSError('fixture persistence failure')
            write_json_object(path, value)
        with patch.object(release, 'materials', return_value={'p12': self.root / 'release.p12'}), \
                patch.object(release, 'tools', return_value={}), \
                patch.object(release, 'verify', side_effect=lambda *args: {**report,
                    'sha256': release.hashlib.sha256(self.hap.read_bytes()).hexdigest()}), \
                patch.object(release.pgyer_upload, 'get_cos_token', return_value=('https://fixture.test', 'build', {})) as token, \
                patch.object(release.pgyer_upload, '_post_multipart', side_effect=post) as send, \
                patch.object(release.pgyer_upload, '_get_json', return_value=published) as query:
            with patch('delivery.delivery.write_json_object', side_effect=fail_final_receipt):
                with self.assertRaises(OSError):
                    release.upload(self.hap, output, config, config_path)
            original = self.hap.read_bytes()
            self.hap.write_bytes(original + b'changed signature')
            with self.assertRaisesRegex(release.DeliveryContextError, '禁止重新上传或发送'):
                release.upload(self.hap, output, config, config_path)
            self.hap.write_bytes(original)
            shutil.rmtree(output)
            self.assertTrue(release.upload(self.hap, output, config, config_path)['distributed'])
            self.assertEqual(['https://fixture.test', release.pgyer_upload.HARMONY_CERT_URL],
                             [call.args[0] for call in send.call_args_list])
            token.assert_called_once()
            query.assert_called_once()

    def notification_records(self):
        self.write_hap(build_type='QA')
        config = {**self.config, **release.build_settings('QA'), 'dryRun': False,
                  'branch': 'dev/cihang', 'tag': '', 'commit': 'a' * 40, 'notify': True}
        provenance = {key: config[key] for key in ('brand', 'buildType', 'branch', 'tag', 'commit')}
        provenance.update(sha256=release.hashlib.sha256(self.hap.read_bytes()).hexdigest(), dryRun=False, distributed=True)
        report = {**release.hap_metadata(self.hap, config), **provenance,
                  'appVerified': True, 'profileVerified': True, 'uiEngine': 'kuikly'}
        info = {**release.ArtifactInfo('慈航中医', '1.0.0', '1', self.config['bundleName'], 'build',
                                      'https://www.pgyer.com/abc', '', 'kuikly').to_dict(),
                **provenance, 'platform': 'harmonyos'}
        config_path = self.root / 'brands.json'
        config_path.write_text(json.dumps({'cihang': {'displayName': '慈航中医',
                                                      'testers': [{'name': '测试', 'mobileEnv': 'TESTER_MOBILE'}]}}))
        output = self.root / 'output'
        output.mkdir()
        (output / 'release-info.json').write_text(json.dumps(info))
        (output / 'signature-report.json').write_text(json.dumps(report))
        scope = {'operation': 'harmony-distribution', 'platform': 'harmonyos', 'brand': config['brand']}
        namespace, legacy = release.delivery_namespace('pgyer')
        identity = {**scope, 'artifact_sha256': info['sha256'], 'commit': config['commit'], 'branch': config['branch'],
                    'ui_engine': 'kuikly', 'environment': config['buildType'], 'certificate_sha256': 'c' * 64}
        with release.delivery_operation(identity, scope=scope, namespace=namespace, legacy_compatible=legacy) as (_, save):
            save(phase='complete', build_key='build', receipt=release.ArtifactInfo.from_dict(info).to_dict())
        with release.delivery_operation({'operation': 'pgyer-certificate', 'build_key': 'build'}, namespace=namespace, legacy_compatible=legacy) as (_, save):
            save(phase='completed', certificate_sha256='c' * 64)
        return config, config_path, output, info, report

    def test_notify_reuses_brand_config_and_harmony_platform_only_after_distribution(self):
        config, config_path, output, _info, _report = self.notification_records()
        with patch.object(release.dingtalk_notify, 'send_release_notification') as send, \
                patch.dict(os.environ, {'TESTER_MOBILE': '12345678901', 'DINGTALK_WEBHOOK': 'webhook', 'DINGTALK_SECRET': 'secret'}):
            result = release.notify(self.hap, output, config, config_path, changelog='本次更新', builder='builder')
            self.assertTrue(result['notified'])
            args = send.call_args.kwargs
            self.assertEqual(('HarmonyOS', 'kuikly', 'test', 'dev/cihang', 'a' * 40),
                             tuple(args[key] for key in ('platform', 'ui_engine', 'environment', 'branch', 'commit')))
            self.assertEqual('12345678901', args['brand'].testers[0].mobile)
            self.assertEqual('kuikly', args['release'].ui_engine)
            self.assertEqual(_info['sha256'], args['artifact_sha256'])
            send.reset_mock()
            self.assertFalse(release.notify(self.hap, output, {**config, 'dryRun': True}, config_path)['notified'])
            send.assert_not_called()

    def test_notify_rejects_incomplete_stale_or_wrong_platform_reports_before_send(self):
        config, config_path, output, info, report = self.notification_records()
        cases = [
            ('release-info.json', 'distributed', value) for value in (False, 'true', 1)
        ] + [
            ('signature-report.json', 'distributed', False), ('release-info.json', 'dryRun', True),
            ('signature-report.json', 'dryRun', 'false'), ('signature-report.json', 'appVerified', 1),
            ('signature-report.json', 'profileVerified', False), ('release-info.json', 'brand', 'jimin'),
            ('release-info.json', 'buildType', 'Release'), ('signature-report.json', 'branch', 'feature/cihang/old'),
            ('release-info.json', 'commit', 'b' * 40), ('signature-report.json', 'commit', ''),
            ('signature-report.json', 'sha256', 'b' * 64), ('release-info.json', 'sha256', 'b' * 64),
            ('signature-report.json', 'versionName', '9.9.9'), ('release-info.json', 'version_code', '9'),
            ('signature-report.json', 'versionCode', True), ('signature-report.json', 'debugLogging', 1),
            ('release-info.json', 'package_name', 'wrong'), ('release-info.json', 'platform', 'android'),
            ('release-info.json', 'ui_engine', 'cmp'), ('signature-report.json', 'uiEngine', 'cmp'),
            ('release-info.json', 'build_key', ''), ('release-info.json', 'build_key', 'DRY-RUN'),
            ('release-info.json', 'download_url', ''), ('release-info.json', 'download_url', 'http://www.pgyer.com/abc'),
            ('release-info.json', 'download_url', 'https://attacker.example/abc'),
            ('release-info.json', 'download_url', 'https://user:password@www.pgyer.com/abc'),
            ('release-info.json', 'download_url', 'https://www.pgyer.com/abc\n'),
        ]
        with patch.object(release.dingtalk_notify, 'send_release_notification') as send:
            for filename, key, value in cases:
                with self.subTest(filename=filename, key=key, value=value):
                    (output / 'release-info.json').write_text(json.dumps(info))
                    (output / 'signature-report.json').write_text(json.dumps(report))
                    original = info if filename == 'release-info.json' else report
                    (output / filename).write_text(json.dumps({**original, key: value}))
                    with self.assertRaises(release.HarmonyReleaseError):
                        release.notify(self.hap, output, config, config_path)
            send.assert_not_called()

    def test_notify_rejects_missing_tester_credential_and_changed_hap(self):
        config, config_path, output, _info, _report = self.notification_records()
        with patch.object(release.dingtalk_notify, 'send_release_notification') as send, \
                patch.dict(os.environ, {'TESTER_MOBILE': ''}):
            with self.assertRaisesRegex(release.HarmonyReleaseError, 'Credential 不完整'):
                release.notify(self.hap, output, config, config_path)
            with self.hap.open('ab') as file:
                file.write(b'changed')
            with self.assertRaisesRegex(release.HarmonyReleaseError, 'SHA-256'):
                release.notify(self.hap, output, config, config_path)
            send.assert_not_called()

    def test_notify_requires_restored_complete_distribution_and_certificate_ledgers(self):
        config, config_path, output, info, report = self.notification_records()
        restored = self.root / 'restored-state'
        shutil.copytree(self.root / 'state', restored)
        with patch.dict(os.environ, {'DELIVERY_STATE_ROOT': str(restored), 'TESTER_MOBILE': '12345678901'}), patch.object(release.dingtalk_notify, 'send_release_notification') as send:
            self.assertTrue(release.notify(self.hap, output, config, config_path)['notified'])
            send.reset_mock()
            for path in restored.glob('*.json'):
                original = json.loads(path.read_text())
                if original['identity']['operation'] == 'harmony-distribution':
                    mutations = ({'phase':'pending'}, {'identity':{**original['identity'], 'artifact_sha256':'b' * 64}}, {'receipt':{**original['receipt'], 'package_name':'wrong'}})
                else:
                    mutations = ({'phase':'pending'}, {'certificate_sha256':'b' * 64})
                for mutation in mutations:
                    path.write_text(json.dumps({**original, **mutation}))
                    with self.assertRaises(release.HarmonyReleaseError): release.notify(self.hap, output, config, config_path)
                path.write_text(json.dumps(original))
            send.assert_not_called()

    def test_notify_cli_requires_fixed_application_commit_and_catches_secret_errors(self):
        config, config_path, output, _info, _report = self.notification_records()
        arguments = ['notify', '--brand', 'cihang', '--branch', 'dev/cihang', '--build-type', 'QA',
                     '--root', str(self.root), '--config', str(config_path), '--hap', str(self.hap),
                     '--output-dir', str(output), '--dry-run', 'false', '--notify', 'true']
        with patch.object(release.provisioning, 'load_brand', return_value=self.config), \
                patch.object(release, 'run', return_value='a' * 40), \
                patch.object(release.dingtalk_notify, 'send_release_notification') as send, \
                patch.dict(os.environ, {'TESTER_MOBILE': '12345678901'}), patch('builtins.print') as printed:
            self.assertEqual(1, release.main(arguments))
            self.assertEqual(1, release.main(arguments + ['--commit', 'b' * 40]))
            send.assert_not_called()
            self.assertEqual(0, release.main(arguments + ['--commit', config['commit']]))
            send.side_effect = release.dingtalk_notify.DingTalkError('secret-response')
            self.assertEqual(1, release.main(arguments + ['--commit', config['commit']]))
            self.assertNotIn('secret-response', str(printed.call_args))
            self.assertIn('禁止盲目重发', str(printed.call_args))
            send.side_effect = release.DeliveryContextError('必须设置工作区外的绝对 DELIVERY_STATE_ROOT')
            self.assertEqual(1, release.main(arguments + ['--commit', config['commit']]))
            self.assertIn('DELIVERY_STATE_ROOT', str(printed.call_args))

    def test_plan_uses_team_p12_and_brand_profiles(self):
        with patch.object(release.provisioning, 'load_brand', return_value=self.config):
            config = release.plan(self.root / 'config', 'cihang', 'dev/cihang')
            self.assertEqual(config['credentials']['p12'], 'harmony-team-cihang-release-p12')
            self.assertEqual(config['credentials']['profile'], 'harmony-team-cihang-cihang-designated-release-profile')
            with self.assertRaises(release.HarmonyReleaseError):
                release.plan(self.root / 'config', 'cihang', 'dev/jimin')

    def test_formal_plan_accepts_only_matching_harmony_tag_and_release(self):
        with patch.object(release.provisioning, 'load_brand', return_value=self.config):
            formal = release.plan(self.root / 'config', 'cihang', '', pipeline='release', tag='cihang-ohos-v1.0.0')
            self.assertEqual('cihang-ohos-v1.0.0', formal['tag'])
            self.assertEqual('', formal['branch'])
            self.assertEqual('Release', formal['buildType'])
            self.assertEqual('1.0.0', formal['expectedVersion'])
            self.assertEqual('1.0.0', release.hap_metadata(self.hap, formal)['versionName'])
            mismatched = release.plan(self.root / 'config', 'cihang', '', pipeline='release', tag='cihang-ohos-v9.9.9')
            with self.assertRaisesRegex(release.HarmonyReleaseError, 'versionName 与正式 Tag'):
                release.hap_metadata(self.hap, mismatched)
            with patch.object(release, 'tools', return_value={}), patch.object(release, 'materials') as materials, \
                    self.assertRaisesRegex(release.HarmonyReleaseError, 'versionName 与正式 Tag'):
                release.sign(self.hap, self.root / 'signed.hap', mismatched)
            materials.assert_not_called()
            for tag in ('', 'cihang-v1.0.0', 'cihang-ios-v1.0.0', 'jimin-ohos-v1.0.0', 'cihang-ohos-vbad'):
                with self.subTest(tag=tag), self.assertRaises(release.HarmonyReleaseError):
                    release.plan(self.root / 'config', 'cihang', '', pipeline='release', tag=tag)
            for build_type in ('QA', 'Debug'):
                with self.subTest(build_type=build_type), self.assertRaisesRegex(release.HarmonyReleaseError, '只接受 Tag 和 Release'):
                    release.plan(self.root / 'config', 'cihang', '', pipeline='release', tag=formal['tag'], build_type=build_type)
            with self.assertRaises(release.HarmonyReleaseError):
                release.plan(self.root / 'config', 'cihang', 'dev/cihang', pipeline='release', tag=formal['tag'])
            with self.assertRaisesRegex(release.HarmonyReleaseError, '不接受 Tag'):
                release.plan(self.root / 'config', 'cihang', 'dev/cihang', tag=formal['tag'])

    def test_cli_formal_plan_accepts_tag_without_branch(self):
        with patch.object(release.provisioning, 'load_brand', return_value=self.config), patch('builtins.print') as output:
            result = release.main(['plan', '--brand', 'cihang', '--pipeline', 'release', '--tag', 'cihang-ohos-v1.0.0'])
            self.assertEqual(0, result)
            self.assertEqual('cihang-ohos-v1.0.0', json.loads(output.call_args[0][0])['tag'])

    def test_all_build_types_have_independent_mode_diagnostics_and_environment(self):
        for build_type, mode, debug, environment in [('Debug', 'debug', True, 'test'),
                                                     ('QA', 'release', True, 'test'),
                                                     ('Release', 'release', False, 'production')]:
            with self.subTest(build_type=build_type), patch.object(release.provisioning, 'load_brand', return_value=self.config):
                config = release.plan(self.root / 'config', 'cihang', 'dev/cihang', build_type=build_type)
                self.assertEqual((mode, debug, environment), (config['buildMode'], config['debugLogging'], config['defaultApiEnvironment']))
                self.assertEqual('harmony-team-cihang-release-p12', config['credentials']['p12'])
                self.assertEqual('harmony-team-cihang-cihang-designated-release-profile', config['credentials']['profile'])
                self.write_hap(build_type=build_type)
                report = release.hap_metadata(self.hap, config)
                self.assertEqual(build_type, report['buildType'])
                self.assertEqual(environment, report['defaultApiEnvironment'])
        with self.assertRaisesRegex(release.HarmonyReleaseError, 'buildType'):
            release.build_settings('DevRelease')

    def test_hap_actual_compilation_flag_and_packaged_config_must_match_type(self):
        for build_type in ('Debug', 'QA', 'Release'):
            config = {**self.config, **release.build_settings(build_type)}
            with self.subTest(build_type=build_type):
                self.write_hap(build_type=build_type, debug=build_type != 'Debug')
                with self.assertRaisesRegex(release.HarmonyReleaseError, '编译标志'):
                    release.hap_metadata(self.hap, config)
                for key, bad in [('buildType', 'Debug' if build_type != 'Debug' else 'QA'),
                                 ('buildMode', 'invalid'), ('debugLogging', 'true'),
                                 ('defaultApiEnvironment', 'invalid')]:
                    self.write_hap(build_type=build_type, metadata={**release.build_settings(build_type), key: bad})
                    with self.assertRaisesRegex(release.HarmonyReleaseError, '元数据'):
                        release.hap_metadata(self.hap, config)
        self.write_hap()
        with warnings.catch_warnings(), zipfile.ZipFile(self.hap, 'a') as archive:
            warnings.simplefilter('ignore', UserWarning)
            archive.writestr('resources/resfile/ci-build.json', json.dumps(release.build_settings('Release')))
        with self.assertRaisesRegex(release.HarmonyReleaseError, '唯一构建配置'):
            release.hap_metadata(self.hap, self.config)

    def test_hap_build_mode_and_boolean_are_strict(self):
        for debug, mode in [('false', 'release'), (0, 'release'), (False, 'debug')]:
            self.write_hap(debug=debug, mode=mode)
            with self.assertRaisesRegex(release.HarmonyReleaseError, '编译标志'):
                release.hap_metadata(self.hap, self.config)
        for value in ([], {'app': []}, {'app': {}, 'module': None}):
            with zipfile.ZipFile(self.hap, 'w') as archive:
                archive.writestr('module.json', json.dumps(value))
                archive.writestr('resources/resfile/ci-build.json', '{}')
            with self.assertRaisesRegex(release.HarmonyReleaseError, 'module.json'):
                release.hap_metadata(self.hap, self.config)

    def test_cli_signed_artifact_includes_selected_type(self):
        for build_type in ('Debug', 'QA', 'Release'):
            with self.subTest(build_type=build_type), patch.object(release.provisioning, 'load_brand', return_value=self.config), \
                    patch.object(release, 'sign', return_value={'appVerified': True}) as sign, patch('builtins.print'):
                result = release.main(['sign', '--brand', 'cihang', '--branch', 'dev/cihang', '--build-type', build_type,
                                       '--hap', str(self.hap), '--output-dir', str(self.root / 'output')])
                self.assertEqual(0, result)
                self.assertEqual(self.root / 'output' / ('cihang-' + build_type + '-signed.hap'), sign.call_args.args[1])
                self.assertEqual(build_type, sign.call_args.args[2]['buildType'])

    def test_wrong_package_and_app_container_rejected(self):
        self.write_hap(bundle='wrong.bundle')
        with self.assertRaisesRegex(release.HarmonyReleaseError, '包名'):
            release.hap_metadata(self.hap, self.config)
        with self.assertRaisesRegex(release.HarmonyReleaseError, 'HAP'):
            release.hap_metadata(self.root / 'application.app', self.config)

    def test_profile_bytes_restored_on_failure(self):
        profile = self.root / 'profile.json5'
        original = b'{"app":{"signingConfigs":[{"name":"existing"}]}}\n'
        profile.write_bytes(original)
        profile.chmod(0o640)
        with self.assertRaisesRegex(RuntimeError, 'build failed'):
            with release.temporary_json(profile, lambda value: {'different': True}):
                self.assertEqual(json.loads(profile.read_text()), {'different': True})
                raise RuntimeError('build failed')
        self.assertEqual(profile.read_bytes(), original)
        self.assertEqual(profile.stat().st_mode & 0o777, 0o640)

    def fake_memory(self, limit, used=0, nested=None):
        proc = self.root / 'proc'
        group = self.root / 'cgroup'
        (proc / 'self').mkdir(parents=True)
        group.mkdir()
        (proc / 'meminfo').write_text('MemAvailable: 15000000 kB\n')
        (proc / 'self/cgroup').write_text('0::/nested\n')
        (group / 'memory.max').write_text(str(limit))
        (group / 'memory.current').write_text(str(used))
        if nested:
            (group / 'nested').mkdir()
            (group / 'nested/memory.max').write_text(str(nested))
            (group / 'nested/memory.current').write_text('0')
        return proc, group

    def test_cgroup_effective_budget_includes_usage_and_ancestor(self):
        gib = 1024 ** 3
        proc, group = self.fake_memory(7 * gib, used=gib, nested=12 * gib)
        self.assertEqual(release.memory_budget(proc, group), 6 * gib)

    def test_low_memory_fails_before_tools_or_build(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(release.platform, 'system', return_value='Linux'), \
                patch.object(release.platform, 'machine', return_value='x86_64'), \
                patch.object(release, 'memory_budget', return_value=7 * 1024 ** 3), \
                patch.object(release, 'tools') as tools:
            with self.assertRaisesRegex(release.HarmonyReleaseError, '14.00 GiB'):
                release.preflight(self.config)
            tools.assert_not_called()

    def test_ci_heap_settings_require_real_nine_gib_budget(self):
        with patch.dict(os.environ, {'HARMONY_GRADLE_HEAP_MB': '4096', 'HARMONY_NODE_HEAP_MB': '1024'}, clear=True), \
                patch.object(release.platform, 'system', return_value='Linux'), \
                patch.object(release.platform, 'machine', return_value='x86_64'), \
                patch.object(release, 'memory_budget', return_value=8696848384), \
                patch.object(release, 'tools', side_effect=release.HarmonyReleaseError('memory passed')) as tools:
            self.assertEqual(release.memory_settings()['required'], 9 * 1024 ** 3)
            with self.assertRaisesRegex(release.HarmonyReleaseError, '不足 9.00 GiB'):
                release.preflight(self.config)
            tools.assert_not_called()
            with patch.object(release, 'memory_budget', return_value=9 * 1024 ** 3):
                with self.assertRaisesRegex(release.HarmonyReleaseError, 'memory passed'):
                    release.preflight(self.config)
            tools.assert_called_once()

    def test_memory_settings_cannot_remove_native_reserve_or_exceed_node_cap(self):
        for values in ({'HARMONY_GRADLE_HEAP_MB': '3072'}, {'HARMONY_NODE_HEAP_MB': '0'},
                       {'HARMONY_NODE_HEAP_MB': '2049'}, {'HARMONY_GRADLE_HEAP_MB': '4g'}):
            with patch.dict(os.environ, values, clear=True), self.assertRaises(release.HarmonyReleaseError):
                release.memory_settings()

    def test_physical_available_memory_limits_large_cgroup(self):
        proc, group = self.fake_memory(12 * 1024 ** 3)
        (proc / 'meminfo').write_text('MemAvailable: 5000000 kB\n')
        self.assertEqual(release.memory_budget(proc, group), 5000000 * 1024)

    def swap_fixture(self, version):
        gib = 1024 ** 3
        proc, group = self.fake_memory(9 * gib, used=2 * gib)
        (proc / 'meminfo').write_text('MemAvailable: 5242880 kB\nSwapFree: 8388608 kB\n')
        if version == 1:
            (proc / 'self/cgroup').write_text('5:memory:/nested\n')
            memory = group / 'memory'
            memory.mkdir()
            (memory / 'memory.limit_in_bytes').write_text(str(9 * gib))
            (memory / 'memory.usage_in_bytes').write_text(str(2 * gib))
            group = memory
        return proc, group

    def test_v2_swap_allowed_remaining_is_bounded_by_host_and_usage(self):
        proc, group = self.swap_fixture(2)
        (group / 'memory.swap.max').write_text(str(8 * 1024 ** 3))
        (group / 'memory.swap.current').write_text(str(1024 ** 3))
        self.assertEqual(12 * 1024 ** 3, release.memory_budget(proc, group))
        (proc / 'meminfo').write_text('MemAvailable: 5242880 kB\nSwapFree: 1048576 kB\n')
        self.assertEqual(6 * 1024 ** 3, release.memory_budget(proc, group))

    def test_v2_disabled_or_unaccounted_swap_does_not_borrow_host_swap(self):
        proc, group = self.swap_fixture(2)
        self.assertEqual(5 * 1024 ** 3, release.memory_budget(proc, group))
        (group / 'memory.swap.max').write_text('0')
        (group / 'memory.swap.current').write_text('0')
        self.assertEqual(5 * 1024 ** 3, release.memory_budget(proc, group))

    def test_v2_ancestor_swap_prohibition_overrides_nested_permission(self):
        proc, group = self.swap_fixture(2)
        (group / 'memory.swap.max').write_text('0')
        (group / 'memory.swap.current').write_text('0')
        nested = group / 'nested'
        nested.mkdir()
        for name, value in {'memory.max': 9, 'memory.current': 0, 'memory.swap.max': 8, 'memory.swap.current': 0}.items():
            (nested / name).write_text(str(value * 1024 ** 3))
        self.assertEqual(5 * 1024 ** 3, release.memory_budget(proc, group))

    def test_v1_memsw_remaining_counts_only_permitted_unconsumed_swap(self):
        proc, group = self.swap_fixture(1)
        (group / 'memory.memsw.limit_in_bytes').write_text(str(17 * 1024 ** 3))
        (group / 'memory.memsw.usage_in_bytes').write_text(str(5 * 1024 ** 3))
        self.assertEqual(10 * 1024 ** 3, release.memory_budget(proc, group.parent))
        (group / 'memory.swappiness').write_text('0')
        self.assertEqual(5 * 1024 ** 3, release.memory_budget(proc, group.parent))

    def test_v1_equal_limits_or_missing_memsw_do_not_enable_swap(self):
        proc, group = self.swap_fixture(1)
        self.assertEqual(5 * 1024 ** 3, release.memory_budget(proc, group.parent))
        (group / 'memory.memsw.limit_in_bytes').write_text(str(9 * 1024 ** 3))
        (group / 'memory.memsw.usage_in_bytes').write_text(str(2 * 1024 ** 3))
        self.assertEqual(5 * 1024 ** 3, release.memory_budget(proc, group.parent))

    def test_v1_ancestor_memsw_cap_limits_nested_remaining_capacity(self):
        proc, group = self.swap_fixture(1)
        (group / 'memory.memsw.limit_in_bytes').write_text(str(10 * 1024 ** 3))
        (group / 'memory.memsw.usage_in_bytes').write_text(str(3 * 1024 ** 3))
        nested = group / 'nested'
        nested.mkdir()
        for name, value in {'memory.limit_in_bytes': 9, 'memory.usage_in_bytes': 2,
                            'memory.memsw.limit_in_bytes': 17, 'memory.memsw.usage_in_bytes': 2}.items():
            (nested / name).write_text(str(value * 1024 ** 3))
        self.assertEqual(5 * 1024 ** 3, release.memory_budget(proc, group.parent))

    def test_disabled_signing_rejected(self):
        with patch.object(release.platform, 'system', return_value='Linux'), \
                patch.object(release.platform, 'machine', return_value='x86_64'):
            with self.assertRaisesRegex(release.HarmonyReleaseError, 'enabled=false'):
                release.preflight({**self.config, 'enabled': False})

    def test_actual_runtime_probe_rejects_missing_or_invalid_sdk_library(self):
        sdk = self.root / 'sdk'
        image = sdk / 'default/hms/toolchains/lib/libimage_transcoder_shared.so'
        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                if invalid:
                    image.parent.mkdir(parents=True)
                    image.write_bytes(b'not an ELF shared library')
                with patch.dict(os.environ, {'KONAN_DATA_DIR': str(self.root / 'konan')}):
                    with self.assertRaisesRegex(release.HarmonyReleaseError, 'SDK运行库预检失败.*SDK libimage_transcoder_shared.so') as error:
                        release.runtime_preflight({'sdk': sdk})
                self.assertNotIn(str(self.root), str(error.exception))
                self.assertNotIn('not an ELF', str(error.exception))

    def test_system_library_probe_uses_only_child_vendor_search_path(self):
        sdk = self.root / 'sdk'
        previous = 'inherited-private-search-path'
        def command(args, **kwargs):
            self.assertEqual(kwargs['env']['LD_LIBRARY_PATH'], os.pathsep.join([
                str(sdk / 'default/openharmony/previewer/common/bin'), str(sdk / 'default/hms/toolchains/lib')]))
            self.assertIn('libatomic.so.1', args)
            self.assertIn('libxml2.so.2', args)
            self.assertIn('libGL.so.1', args)
            return '[2]'
        with patch.dict(os.environ, {'LD_LIBRARY_PATH': previous}), patch.object(release, 'run', side_effect=command):
            with self.assertRaisesRegex(release.HarmonyReleaseError, 'SDK运行库预检失败.*libGL.so.1'):
                release.runtime_preflight({'sdk': sdk})
            self.assertEqual(previous, os.environ['LD_LIBRARY_PATH'])

    def test_probe_is_required_even_without_native_cache_and_checks_cached_llvm(self):
        konan = self.root / 'konan'
        llvm = konan / 'dependencies/cpf-llvm-19-x86_64-linux-dev-20'
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            if '-c' in args:
                return '[]'
            return 'clang version 19.1.4' if Path(args[0]).name == 'clang++' else 'LLD 19.1.4'
        with patch.dict(os.environ, {'KONAN_DATA_DIR': str(konan)}), patch.object(release, 'run', side_effect=command):
            release.runtime_preflight({'sdk': self.root / 'sdk'})
            self.assertEqual(1, len(calls))
            llvm.mkdir(parents=True)
            release.runtime_preflight({'sdk': self.root / 'sdk'})
            self.assertEqual(['clang++', 'ld.lld'], [Path(args[0]).name for args in calls[-2:]])
            self.assertTrue(all(args[-1] == '--version' for args in calls[-2:]))
        with patch.dict(os.environ, {'KONAN_DATA_DIR': str(konan)}), \
                patch.object(release, 'run', side_effect=['[]', release.HarmonyReleaseError('private loader failure')]):
            with self.assertRaisesRegex(release.HarmonyReleaseError, 'LLVM运行库预检失败.*clang\\+\\+') as error:
                release.runtime_preflight({'sdk': self.root / 'sdk'})
            self.assertNotIn('private loader failure', str(error.exception))

    def test_preflight_runtime_failure_blocks_before_gradle_or_har_install(self):
        paths = {'sdk': self.root / 'sdk', 'node': self.root / 'node'}
        failed = [1]
        def manifest(path):
            return {'version': '26.0.0.105', 'apiVersion': '26'} if path.name == 'oh-uni-package.json' else {
                'version': '26.0.0.630' if path.parent.name == 'ohpm' else '6.26.8'}
        def command(args, **kwargs):
            if args[0] == 'java':
                return 'openjdk version "21.0.1"'
            if args[-1] == '--version':
                return 'v24.14.1'
            return json.dumps(failed)
        with patch.dict(os.environ, {'HARMONY_GRADLE_HEAP_MB': '4096', 'HARMONY_NODE_HEAP_MB': '1024'}), \
                patch.object(release.platform, 'system', return_value='Linux'), \
                patch.object(release.platform, 'machine', return_value='x86_64'), \
                patch.object(release, 'memory_budget', return_value=9 * 1024 ** 3), \
                patch.object(release, 'tools', return_value=paths), patch.object(release.shutil, 'which', return_value='/tool'), \
                patch.object(release, 'read_json', side_effect=manifest), patch.object(release, 'run', side_effect=command) as run:
            with self.assertRaisesRegex(release.HarmonyReleaseError, 'SDK运行库预检失败.*libxml2.so.2'):
                release.build(self.root, self.config)
            self.assertFalse(any('install_release_hars.sh' in call.args[0] or 'assembleHap' in call.args[0] for call in run.call_args_list))
            failed.clear()
            with patch.dict(os.environ, {'KONAN_DATA_DIR': str(self.root / 'absent-cache')}):
                self.assertEqual(paths, release.preflight(self.config))

    def test_java_password_stays_in_private_argfile_and_is_removed(self):
        secret = 'space " quote \\ password'
        def invocation(command, **kwargs):
            self.assertNotIn(secret, ' '.join(command))
            args = Path(command[1][1:])
            self.assertEqual(args.stat().st_mode & 0o777, 0o600)
            self.assertIn('space', args.read_text())
            return ''
        with patch.object(release, 'run', side_effect=invocation):
            release.java_signer({'signer': self.root / 'tool.jar'}, ['-keyPwd', secret], self.root)
        self.assertFalse((self.root / 'java.args').exists())

    def test_jenkins_tmp_bindings_are_read_only_and_staged_with_sdk_extensions(self):
        bound = self.root / 'secretFiles'
        bound.mkdir()
        temporary = self.root / 'private-signing'
        temporary.mkdir(mode=0o700)
        values = {'p12': b'test-p12-bytes', 'profile': b'test-profile-bytes',
                  'devices': json.dumps([{'udid': 'private-device', 'deviceType': 4}]).encode()}
        sources = {}
        environment = {'HARMONY_P12_PASSWORD': 'test-only-password'}
        for index, (key, value) in enumerate(values.items()):
            source = bound / ('file' + str(index) + '.tmp')
            source.write_bytes(value)
            source.chmod(0o400)
            sources[key] = (source, source.stat())
            environment['HARMONY_' + ('P12' if key == 'p12' else 'PROFILE' if key == 'profile' else 'DEVICES') + '_FILE'] = str(source)
        def command(args, **kwargs):
            staged = Path(args[args.index('-in') + 1])
            self.assertEqual(temporary / 'release.p12', staged)
            self.assertEqual(0o600, staged.stat().st_mode & 0o777)
            return 'test certificate chain'
        with patch.dict(os.environ, environment), patch.object(release, 'run', side_effect=command), \
                patch.object(release.provisioning, 'certificate_pem', return_value=b'leaf'), \
                patch.object(release.provisioning, 'certificate_chain', return_value=b'leaf-and-root'), \
                patch.object(release, 'ordered_chain', return_value=b'leaf-and-root'):
            result = release.materials(temporary)
        for key, filename in [('p12', 'release.p12'), ('profile', 'designated-release.p7b'), ('devices', 'devices.json')]:
            self.assertEqual(temporary / filename, result[key])
            self.assertEqual(values[key], result[key].read_bytes())
            self.assertEqual(0o600, result[key].stat().st_mode & 0o777)
            source, before = sources[key]
            self.assertEqual(values[key], source.read_bytes())
            after = source.stat()
            self.assertEqual((before.st_ino, before.st_mtime_ns, before.st_mode),
                             (after.st_ino, after.st_mtime_ns, after.st_mode))
        self.assertEqual(0o700, temporary.stat().st_mode & 0o777)
        self.assertEqual([{'udid': 'private-device', 'deviceType': 4}], result['deviceRows'])

    def verification(self, *, passed=True, embedded=b'approved-profile', certificate=b'expected-cert', marker='verify-app success',
                     expected=b'expected-cert', p12_chain=None, chain_orderer=None):
        profile = self.root / 'approved.p7b'
        profile.write_bytes(b'approved-profile')
        content = {'type': 'release'}
        cert_file = self.root / 'p12-chain.pem'
        cert_file.write_bytes(p12_chain if p12_chain is not None else expected)
        material = {'profile': profile, 'certificate': expected, 'cert': cert_file, 'deviceRows': [{'udid': 'private-device'}]}
        def signer(paths, args, temporary):
            if args[0] == 'verify-app':
                (temporary / 'embedded.cer').write_bytes(certificate)
                (temporary / 'embedded.p7b').write_bytes(embedded)
                return '[INFO] ' + marker + '\n'
            (temporary / 'profile-result.json').write_text(json.dumps({'verifiedPassed': passed, 'content': content}))
            return ''
        with patch.object(release, 'java_signer', side_effect=signer), \
                patch.object(release.provisioning, 'certificate_chain', side_effect=lambda value: value), \
                patch.object(release, 'ordered_chain', side_effect=chain_orderer or (lambda leaf, chain: chain)), \
                patch.object(release, 'certificate_der', side_effect=lambda value: value), \
                patch.object(release.provisioning, 'validate_profile', return_value=content):
            return release.verify(self.hap, self.config, {}, material, self.root)

    def test_verified_report_contains_no_device_identifiers(self):
        result = self.verification()
        self.assertIs(result['appVerified'], True)
        self.assertIs(result['profileVerified'], True)
        self.assertEqual(result['deviceCount'], 1)
        self.assertNotIn('private-device', json.dumps(result))
        self.assertIs(result['distributed'], False)
        self.assertEqual(('Release', 'release', False, 'production'),
                         tuple(result[key] for key in ('buildType', 'buildMode', 'debugLogging', 'defaultApiEnvironment')))

    def test_profile_boolean_must_be_true_not_string(self):
        for value in ('true', False, 1, None):
            with self.assertRaisesRegex(release.HarmonyReleaseError, '严格 true'):
                self.verification(passed=value)

    def test_embedded_profile_and_certificate_must_match(self):
        with self.assertRaisesRegex(release.HarmonyReleaseError, '批准材料'):
            self.verification(embedded=b'wrong-profile')
        with self.assertRaisesRegex(release.HarmonyReleaseError, '团队 P12'):
            self.verification(certificate=b'wrong-cert')

    def test_sdk_success_marker_required(self):
        with self.assertRaisesRegex(release.HarmonyReleaseError, '明确成功'):
            self.verification(marker='verify-app failed')

    def test_sdk_root_first_chain_requires_exact_expected_leaf_and_p12_chain(self):
        def block(name):
            return b'-----BEGIN CERTIFICATE-----' + name.encode() + b'-----END CERTIFICATE-----'
        leaf, middle, root, wrong, extra = [block(name) for name in ('leaf', 'middle', 'root', 'wrong', 'extra')]
        relations = {leaf: ('leaf', 'middle'), middle: ('middle', 'root'), root: ('root', 'root'),
                     wrong: ('wrong', 'middle'), extra: ('extra', 'extra')}
        def openssl(*args, data):
            subject, issuer = relations[data]
            return ('name=' + (issuer if '-issuer' in args else subject)).encode()
        p12_chain = b'\n'.join([leaf, middle, root])
        with patch.object(release.provisioning, 'openssl', side_effect=openssl):
            result = self.verification(certificate=b'\n'.join([root, middle, leaf]), expected=leaf,
                                       p12_chain=p12_chain, chain_orderer=release.ordered_chain)
            self.assertIs(result['appVerified'], True)
            self.assertIs(result['profileVerified'], True)
            for invalid in ([root, middle], [root, middle, wrong], [root, middle, leaf, extra],
                            [root, middle, leaf, leaf], [leaf, middle]):
                with self.subTest(chain=invalid), self.assertRaisesRegex(release.HarmonyReleaseError, '团队 P12'):
                    self.verification(certificate=b'\n'.join(invalid), expected=leaf,
                                      p12_chain=p12_chain, chain_orderer=release.ordered_chain)
            # SDK 含正确 leaf 仍不能接受 P12 缺少根证书的材料。
            with self.assertRaisesRegex(release.HarmonyReleaseError, '团队 P12'):
                self.verification(certificate=b'\n'.join([root, middle, leaf]), expected=leaf,
                                  p12_chain=b'\n'.join([leaf, middle]), chain_orderer=release.ordered_chain)

    def test_chain_requires_contiguous_leaf_to_root(self):
        def block(name):
            return b'-----BEGIN CERTIFICATE-----' + name.encode() + b'-----END CERTIFICATE-----'
        leaf, middle, root = [block(name) for name in ('leaf', 'middle', 'root')]
        relations = {leaf: ('leaf', 'middle'), middle: ('middle', 'root'), root: ('root', 'root')}
        def openssl(*args, data):
            subject, issuer = relations[data]
            return ('name=' + (issuer if '-issuer' in args else subject)).encode()
        with patch.object(release, 'certificate_der', side_effect=lambda value: value), \
                patch.object(release.provisioning, 'openssl', side_effect=openssl):
            self.assertEqual(release.ordered_chain(leaf, b'\n'.join([root, leaf, middle])),
                             b'\n'.join([leaf, middle, root]) + b'\n')
            with self.assertRaisesRegex(release.HarmonyReleaseError, '完整发布证书链'):
                release.ordered_chain(leaf, leaf)
            with self.assertRaisesRegex(release.HarmonyReleaseError, '不连续'):
                release.ordered_chain(leaf, b'\n'.join([leaf, root]))

    def test_build_limits_node_and_restores_json5_after_compile_failure(self):
        app = self.root / 'ohosApp'
        (app / 'hvigor').mkdir(parents=True)
        profile = app / 'build-profile.json5'
        profile_original = b'{"app":{"signingConfigs":[{"name":"existing"}],"products":[{"name":"cihang","signingConfig":"existing"}]}}\n'
        profile.write_bytes(profile_original)
        hvigor = app / 'hvigor/hvigor-config.json5'
        hvigor_original = b'{\n// comment to preserve\n"modelVersion":"26.0.0"\n}\n'
        hvigor.write_bytes(hvigor_original)
        paths = {name: self.root / name for name in ('node', 'sdk', 'ohpm')}
        paths['hvigor'] = self.root / 'clt/hvigor/bin/hvigorw.js'
        def command(args, **kwargs):
            if '-e' in args:
                return '{"modelVersion":"26.0.0"}'
            if 'assembleHap' in args:
                self.assertIn('--no-daemon', args)
                self.assertIn('--no-parallel', args)
                self.assertIn('buildMode=' + self.config['buildMode'], args)
                self.assertIn('buildType=' + self.config['buildType'], args)
                self.assertEqual(kwargs['env']['OHOS_BUILD_MODE'], self.config['buildMode'])
                self.assertEqual(kwargs['env']['NODE_OPTIONS'], '--max-old-space-size=1024')
                self.assertEqual(kwargs['env']['HARMONY_GRADLE_HEAP_MB'], '4096')
                self.assertEqual(kwargs['env']['HARMONY_NODE_HEAP_MB'], '1024')
                self.assertIn('--max-old-space-size=1024', args)
                self.assertEqual(json.loads(hvigor.read_text())['nodeOptions']['maxOldSpaceSize'], 1024)
                self.assertNotIn('signingConfig', json.loads(profile.read_text())['app']['products'][0])
                raise release.HarmonyReleaseError('compile failed')
            return ''
        with patch.dict(os.environ, {'HARMONY_GRADLE_HEAP_MB': '4096', 'HARMONY_NODE_HEAP_MB': '1024'}), \
                patch.object(release, 'preflight', return_value=paths), patch.object(release, 'run', side_effect=command):
            for build_type in ('Debug', 'QA', 'Release'):
                self.config.update(release.build_settings(build_type))
                with self.subTest(build_type=build_type), self.assertRaisesRegex(release.HarmonyReleaseError, 'compile failed'):
                    release.build(self.root, self.config)
                self.assertEqual(profile.read_bytes(), profile_original)
                self.assertEqual(hvigor.read_bytes(), hvigor_original)


if __name__ == '__main__':
    unittest.main()
    def test_build_failure_diagnostics_are_private_and_not_in_error(self):
        directory = self.root / 'diagnostics'
        failed = subprocess.CompletedProcess(['bash'], 1, stdout='private output', stderr='private error')
        with patch.object(release.subprocess, 'run', return_value=failed):
            with self.assertRaises(release.HarmonyReleaseError) as error:
                release.run(['bash'], failure_dir=directory)
        self.assertNotIn('private', str(error.exception))
        files = list(directory.glob('*.log'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(files[0].read_text(), 'private outputprivate error')
