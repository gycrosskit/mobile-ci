import io
import json
import tarfile
import unittest
from unittest.mock import patch
from subprocess import CompletedProcess
from signing.export_signing_backup import collect_materials, entries, snapshot
from project_config import load_project
from tests.jenkins.test_backup_resources import compose_fixture


class ExportSigningBackupTests(unittest.TestCase):
    @staticmethod
    def tar_bytes(files):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w') as archive:
            for name, content in files.items():
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(content), 0o600
                archive.addfile(info, io.BytesIO(content))
        return output.getvalue()

    def test_project_scope_reads_only_approved_materials_never_controller(self):
        project = load_project()
        project['id'] = 'example-mobile'
        project['backup'].update(scope='project', recipientAuthority='project-admin')
        project['signingBackup'].update(scope='project_signing', prefixes=['ios/test-team/materials/demo'])
        config = compose_fixture(project)
        raw = self.tar_bytes({'ios/test-team/materials/demo/profile.mobileprovision': b'fake profile'})
        with patch('signing.export_signing_backup.subprocess.run', return_value=CompletedProcess([], 0, raw, b'')) as run:
            result = collect_materials(['ssh', 'fixture'], project, config)
        self.assertEqual({'signing-state/ios/test-team/materials/demo/profile.mobileprovision', 'backup-scope.json'}, set(result))
        self.assertEqual(1, run.call_count)
        command = run.call_args.args[0][-1]
        self.assertIn('example-mobile-maintenance', command)
        self.assertIn('ios/test-team/materials/demo', command)
        self.assertNotIn('/var/jenkins_home', command)
        self.assertNotIn('credentials.xml', command)
        self.assertEqual('project_signing', json.loads(result['backup-scope.json'])['scope'])

    def test_project_scope_rejects_other_folder_or_global_secret_response(self):
        project = load_project()
        project['id'] = 'example-mobile'
        project['signingBackup'].update(scope='project_signing', prefixes=['ios/test-team/materials/demo'])
        for name in ('controller/secrets/master.key', 'ios/other-team/materials/other/profile', '../secrets/master.key'):
            with self.subTest(name=name), patch('signing.export_signing_backup.subprocess.run',
                    return_value=CompletedProcess([], 0, self.tar_bytes({name:b'fake secret'}), b'')), self.assertRaises(ValueError):
                collect_materials(['ssh', 'fixture'], project, compose_fixture(project))
        project['signingBackup']['prefixes'] = []
        with patch('signing.export_signing_backup.subprocess.run') as run, self.assertRaises(ValueError):
            collect_materials(['ssh', 'fixture'], project, compose_fixture(project))
        run.assert_not_called()

    def test_controller_dr_preserves_complete_decryption_materials(self):
        project = load_project()
        secrets = {'credentials.xml':b'fake credentials', 'config.xml':b'fake config',
                   'secrets/master.key':b'fake master', 'secrets/hudson.util.Secret':b'fake secret',
                   'jobs/other-folder/config.xml':b'fake other folder'}
        responses = [CompletedProcess([],0,self.tar_bytes(secrets),b''),
                     CompletedProcess([],0,self.tar_bytes({'ios/team/profile':b'fake profile'}),b'')]
        with patch('signing.export_signing_backup.subprocess.run', side_effect=responses):
            result = collect_materials(['ssh', 'fixture'], project, compose_fixture(project))
        self.assertTrue({'controller/' + name for name in secrets} <= set(result))
        self.assertEqual('controller_dr', json.loads(result['backup-scope.json'])['scope'])

    def test_signing_archive_refuses_public_file_permissions(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w') as archive:
            info = tarfile.TarInfo('profile'); info.mode = 0o644
            archive.addfile(info)
        with self.assertRaisesRegex(ValueError, '私有'):
            entries(output.getvalue(), 'signing-state', private=True)

    def test_snapshot_preserves_bytes_and_manifest(self):
        raw = snapshot({"controller/config.xml": b"private", "signing-state/device.json": b"{}"})
        restored = entries(raw, "restored")
        self.assertEqual(restored["restored/controller/config.xml"], b"private")
        manifest = json.loads(restored["restored/manifest.json"])
        self.assertEqual(manifest["controller/config.xml"]["bytes"], 7)

    def test_traversal_and_links_are_rejected(self):
        for name, kind in [("../secret", tarfile.REGTYPE), ("/secret", tarfile.REGTYPE),
                           ("link", tarfile.SYMTYPE), ("hardlink", tarfile.LNKTYPE)]:
            with self.subTest(name=name, kind=kind):
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w") as archive:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    info.linkname = "secret"
                    archive.addfile(info)
                with self.assertRaises(ValueError):
                    entries(buffer.getvalue(), "controller")


if __name__ == "__main__":
    unittest.main()
