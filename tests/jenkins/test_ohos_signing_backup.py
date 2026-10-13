import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import tarfile
import tempfile
import unittest


REMOTE = Path(__file__).resolve().parents[2] / 'jenkins/remote'
HOME_VOLUME = 'sxmqlive_jenkins_home'
SIGNING_VOLUME = 'sxmqlive_ohos_signing'
AGENT_WORK_VOLUME = 'sxmqlive_jenkins_agent_work'
OHOS_AGENT = 'sxmqlive-jenkins-ohos-maintenance-agent'


class OhosSigningBackupTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.remote = REMOTE
        self.work_volume = AGENT_WORK_VOLUME
        self.log = self.root / 'docker.log'
        self.volumes = self.root / 'volumes.json'
        self.volumes.write_text('[]')
        self.home = self.root / 'home'
        self.home.mkdir()
        (self.home / 'config.xml').write_text('controller')
        self.agent_work = self.root / 'agent-work'
        self.state = self.agent_work / 'delivery-state'
        self.state.mkdir(parents=True, mode=0o700)
        self.receipt = self.state / ('a' * 64 + '.json')
        self.receipt.write_text(json.dumps({'version': 1, 'identity': {'operation': 'fixture'}, 'phase': 'pending'}))
        self.receipt.chmod(0o600)
        (self.agent_work / 'workspace').mkdir()
        (self.agent_work / 'workspace/source.kt').write_text('never include workspace')
        docker = self.root / 'docker'
        docker.write_text('''#!/usr/bin/env python3
import io, json, os, pathlib, stat, subprocess, sys, tarfile
args = sys.argv[1:]
with open(os.environ['DOCKER_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
volume_file = pathlib.Path(os.environ['FAKE_VOLUMES'])
volumes = json.loads(volume_file.read_text())
if args[0] == 'compose':
    roles = ['controller', 'android-agent', 'pr-agent', 'ohos-maintenance']
    config = {'name': 'sxmqlive', 'volumes': {}, 'services': {}}
    for role, container in zip(roles, ['sxmqlive-jenkins-controller', 'sxmqlive-jenkins-android-agent', 'sxmqlive-jenkins-pr-agent', 'sxmqlive-jenkins-ohos-maintenance-agent']):
        config['services'][role] = {'container_name': container, 'image': 'sxmqlive-jenkins-controller:latest', 'environment': {'JENKINS_AGENT_WORKDIR': '/home/jenkins/agent', 'DELIVERY_STATE_ROOT': '/home/jenkins/agent/delivery-state'}, 'volumes': []}
    for role, target, name in [('controller','/var/jenkins_home','sxmqlive_jenkins_home'), ('android-agent','/home/jenkins/.dws-state','sxmqlive_dws_state'), ('android-agent','/home/jenkins/agent','sxmqlive_jenkins_agent_work'), ('ohos-maintenance','/home/jenkins/signing','sxmqlive_ohos_signing')]:
        config['volumes'][name] = {'name': name}
        config['services'][role]['volumes'].append({'type':'volume','source':name,'target':target})
    if os.environ.get('FAKE_COMPOSE'):
        config = json.loads(pathlib.Path(os.environ['FAKE_COMPOSE']).read_text())
    print('\\n'.join(config['services']) if '--services' in args else json.dumps(config))
    sys.exit(0)
if args[:2] == ['volume', 'inspect']:
    if '--format' in args and args[-1] in volumes:
        print(os.environ['FAKE_AGENT_WORK'] if args[-1] == os.environ.get('FAKE_WORK_VOLUME', 'sxmqlive_jenkins_agent_work') else os.environ['FAKE_HOME'])
    sys.exit(0 if args[-1] in volumes else 1)
if args[:2] == ['volume', 'create']:
    if args[-1] == os.environ.get('FAIL_CREATE'):
        sys.exit(19)
    volumes.append(args[-1])
    volume_file.write_text(json.dumps(volumes))
elif args[:2] == ['volume', 'rm']:
    volumes.remove(args[-1])
    volume_file.write_text(json.dumps(volumes))
elif args[0] == 'inspect':
    if args[-1] == 'sxmqlive-jenkins-ohos-maintenance-agent':
        if os.environ.get('OHOS_RUNNING') == 'missing':
            sys.exit(1)
        print(os.environ.get('OHOS_RUNNING', 'true'))
    else:
        print(os.environ.get('CONTROLLER_RUNNING', 'false') if args[-1] == 'sxmqlive-jenkins-controller'
              else os.environ.get('ANDROID_RUNNING', 'false'))
elif args[0] == 'start' and args[-1] == os.environ.get('FAIL_START'):
    sys.exit(24)
elif args[0] == 'run' and args[args.index('--entrypoint') + 1] == 'bash' and any('linux-delivery-state.tar.gz' in arg for arg in args):
    if args[-2] == 'restore':
        source = next(args[i + 1].split(':')[0] for i, value in enumerate(args) if value == '--volume' and ':/backup' in args[i + 1])
        volume = next(args[i + 1].split(':')[0] for i, value in enumerate(args) if value == '--volume' and ':/target' in args[i + 1])
        target = pathlib.Path(os.environ['FAKE_RESTORE_ROOT']) / volume / pathlib.Path(args[-1]).parent
        target.mkdir(parents=True, exist_ok=True)
        sys.exit(subprocess.call(['tar', '-C', str(target), '-xzf', str(pathlib.Path(source) / 'linux-delivery-state.tar.gz')]))
    state = pathlib.Path(os.environ['FAKE_AGENT_WORK']) / args[-2]
    if (state.is_symlink() or not state.is_dir() or stat.S_IMODE(state.stat().st_mode) != 0o700
            or os.environ.get('FAKE_STATE_UID', args[-1]) != args[-1]):
        sys.exit(31)
    files = list(state.iterdir())
    if (len(files) > 10000 or any(p.is_symlink() or not p.is_file() or p.stat().st_nlink != 1
                                or stat.S_IMODE(p.stat().st_mode) != 0o600 or p.stat().st_size > 1024 * 1024 for p in files)
            or sum(p.stat().st_size for p in files) > 64 * 1024 * 1024):
        sys.exit(31)
    target = next(args[i + 1].split(':')[0] for i, value in enumerate(args)
                  if value == '--volume' and ':/backup' in args[i + 1])
    def container_owner(member):
        member.uid = member.gid = int(args[-1])
        member.uname = member.gname = ''
        return member
    with tarfile.open(pathlib.Path(target) / 'linux-delivery-state.tar.gz', 'w:gz') as bundle:
        bundle.add(state, arcname='delivery-state', filter=container_owner)
elif args[0] == 'run' and args[args.index('--entrypoint') + 1] == 'tar':
    archive = next(value for value in args if value.startswith('/backup/'))
    if archive == '/backup/ohos-signing.tar.gz' and os.environ.get('FAIL_SIGNING_TAR') == 'true':
        sys.exit(23)
    if '-xzf' in args and os.environ.get('FAKE_RESTORE_ROOT'):
        source = next(args[i + 1].split(':')[0] for i, value in enumerate(args)
                      if value == '--volume' and ':/backup' in args[i + 1])
        volume = next(args[i + 1].split(':')[0] for i, value in enumerate(args)
                      if value == '--volume' and ':/target' in args[i + 1])
        target = pathlib.Path(os.environ['FAKE_RESTORE_ROOT']) / volume
        target.mkdir(parents=True, exist_ok=True)
        sys.exit(subprocess.call(['tar', '-C', str(target), '-xzf', str(pathlib.Path(source) / pathlib.Path(archive).name)]))
    if '-czf' in args:
        target = next(args[i + 1].split(':')[0] for i, value in enumerate(args)
                      if value == '--volume' and ':/backup' in args[i + 1])
        if archive == '/backup/jenkins-home.tar.gz':
            if os.environ.get('CORRUPT_HOME') == 'true':
                (pathlib.Path(target) / 'jenkins-home.tar.gz').write_bytes(b'broken gzip')
                sys.exit(0)
            policy = next(args[i + 1].split(':')[0] for i, value in enumerate(args)
                          if value == '--volume' and ':/home-excludes.txt:' in args[i + 1])
            if sys.platform == 'darwin':
                # macOS 的宿主 tar 不支持容器 GNU tar 的 --anchored；Linux 验证执行真实命令。
                excluded = pathlib.Path(policy).read_text().splitlines()
                def keep(member):
                    return None if any(member.name == p or member.name.startswith(p + '/') for p in excluded) else member
                with tarfile.open(pathlib.Path(target) / 'jenkins-home.tar.gz', 'w:gz') as bundle:
                    bundle.add(os.environ['FAKE_HOME'], arcname='.', filter=keep)
                sys.exit(0)
            command = ['tar', '--exclude-from=' + policy, '-C', os.environ['FAKE_HOME'],
                       '-czf', str(pathlib.Path(target) / 'jenkins-home.tar.gz'), '.']
            command.insert(1, '--anchored')
            sys.exit(subprocess.call(command))
        with tarfile.open(pathlib.Path(target) / pathlib.Path(archive).name, 'w:gz') as bundle:
            item = tarfile.TarInfo('state.json')
            item.size = 2
            bundle.addfile(item, io.BytesIO(b'{}'))
''', encoding='utf-8')
        docker.chmod(0o700)
        self.env = {**os.environ, 'PATH': str(self.root) + os.pathsep + os.environ['PATH'],
                    'DOCKER_LOG': str(self.log), 'FAKE_VOLUMES': str(self.volumes),
                    'FAKE_HOME': str(self.home),
                    'FAKE_AGENT_WORK': str(self.agent_work),
                    'COPYFILE_DISABLE': '1',
                    'JENKINS_SKIP_DWS_STATE': 'true', 'JENKINS_BACKUP_DIR': str(self.root / 'backups')}

    def execute(self, name, *arguments, **environment):
        if name == 'backup.sh' and environment.get('MISSING_AGENT_WORK') != 'true':
            volumes = json.loads(self.volumes.read_text())
            if self.work_volume not in volumes:
                self.volumes.write_text(json.dumps(volumes + [self.work_volume]))
        return subprocess.run(['bash', str(self.remote / name), *map(str, arguments)],
                              env={**self.env, **environment}, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)

    def test_second_project_backup_restore_uses_its_exact_resources_and_receipt_owner(self):
        from project_config import load_project, validate_project
        from tests.jenkins.test_backup_resources import compose_fixture
        project = load_project()
        project['id'] = 'example-mobile'
        for role, job in project['jobs'].items():
            job['name'] = 'example-app/' + role
        for key, provider in project['providers'].items():
            provider['accountId'] = 'example-' + key
            provider['legacyNamespace'] = False
        project['runtime'].update(linuxUid=2001, roles={key: 'example-' + key for key in project['runtime']['roles']})
        project['signingBackup'].update(scope='project_signing', root='/private/signing', prefixes=['ios/test-team/materials/demo'])
        project['signingBackup'].pop('legacyRecipientSha256', None)
        validate_project(project)
        config = compose_fixture(project)
        config['services']['example-android']['environment']['DELIVERY_STATE_ROOT'] = '/home/jenkins/agent/receipts/delivery-state'
        fixture = self.root / 'compose.json'
        fixture.write_text(json.dumps(config))
        ci = self.root / 'fixture-ci'
        self.remote = ci / 'jenkins/remote'
        shutil.copytree(REMOTE, self.remote)
        shutil.copytree(REMOTE.parents[1] / 'delivery', ci / 'delivery')
        for name in ('project.py', 'project_config.py'):
            shutil.copyfile(REMOTE.parents[1] / name, ci / name)
        (ci / 'projects').mkdir()
        (ci / 'projects/example-mobile.json').write_text(json.dumps(project))
        self.work_volume = 'example-mobile-work'
        nested = self.agent_work / 'receipts'
        nested.mkdir(mode=0o700)
        self.state.rename(nested / 'delivery-state')
        self.state = nested / 'delivery-state'
        self.receipt = self.state / self.receipt.name
        self.env.update(JENKINS_CI_PROJECT='example-mobile', FAKE_COMPOSE=str(fixture), FAKE_WORK_VOLUME=self.work_volume)
        self.volumes.write_text(json.dumps(['example-mobile-home', 'example-mobile-dws', 'example-mobile-signing']))
        result = self.execute('backup.sh')
        self.assertEqual(0, result.returncode, result.stderr)
        bundle = next((self.root / 'backups').glob('example-mobile-*.tar.gz'))
        with tarfile.open(bundle) as archive:
            policy = json.load(archive.extractfile('controller-backup-policy.json'))
            self.assertEqual({'version':1,'scope':'controller_dr','projectId':'example-mobile','linuxUid':2001,'receiptRelative':'receipts/delivery-state'}, policy)
        self.volumes.write_text('[]')
        output = self.root / 'restored-example'
        result = self.execute('restore.sh', bundle, FAKE_RESTORE_ROOT=str(output))
        self.assertEqual(3, result.returncode, result.stderr)  # 没有伪造 Mac 回执，明确保持未就绪。
        self.assertEqual(self.receipt.read_bytes(), (output / self.work_volume / 'receipts/delivery-state' / self.receipt.name).read_bytes())
        self.assertTrue(all(not any('sxmqlive' in str(value) for value in command) for command in self.commands() if command[0] in ('stop','start','run','volume')))
        self.volumes.write_text('[]')
        self.env['JENKINS_CI_PROJECT'] = 'sxmqlive'
        fixture.write_text(json.dumps(compose_fixture(load_project())))
        (ci / 'projects/sxmqlive.json').write_text((REMOTE.parents[1] / 'projects/sxmqlive.json').read_text())
        result = self.execute('restore.sh', bundle)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('scope/project/owner differs', result.stderr)
        self.assertEqual([], json.loads(self.volumes.read_text()))

    def commands(self):
        return [json.loads(row) for row in self.log.read_text().splitlines()]

    def migration(self, signing=True, signing_checksum=True):
        files = {'jenkins-home.tar.gz': b'home'}
        if signing:
            files['ohos-signing.tar.gz'] = b'signing'
        manifest = ''.join(hashlib.sha256(value).hexdigest() + '  ' + name + '\n'
                           for name, value in files.items()
                           if name != 'ohos-signing.tar.gz' or signing_checksum)
        files['manifest.sha256'] = manifest.encode()
        archive = self.root / 'migration.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            for name, value in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(value)
                bundle.addfile(info, io.BytesIO(value))
        return archive

    def mac_archive(self, empty=False):
        path = self.root.resolve() / 'mac-delivery-state.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            root = tarfile.TarInfo('delivery-state')
            root.type, root.uid, root.mode = tarfile.DIRTYPE, 502, 0o700
            archive.addfile(root)
            if not empty:
                content = self.receipt.read_bytes()
                item = tarfile.TarInfo('delivery-state/' + self.receipt.name)
                item.uid, item.mode, item.size = 502, 0o600, len(content)
                archive.addfile(item, io.BytesIO(content))
        path.chmod(0o600)
        return path

    def test_delivery_backup_captures_only_private_linux_state_and_checksums_coverage(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME]))
        result = self.execute('backup.sh', ANDROID_RUNNING='true')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('Partial migration', result.stdout)
        with tarfile.open(next((self.root / 'backups').glob('*.tar.gz'))) as outer:
            coverage = json.loads(outer.extractfile('delivery-state-coverage.json').read())
            self.assertEqual({'version': 1, 'linux': 'linux-delivery-state.tar.gz', 'mac': None, 'macSourceUid': None}, coverage)
            checksum = outer.extractfile('manifest.sha256').read().decode('utf-8')
            for name in ('linux-delivery-state.tar.gz', 'delivery-state-coverage.json'):
                self.assertIn(hashlib.sha256(outer.extractfile(name).read()).hexdigest() + '  ' + name, checksum)
            with tarfile.open(fileobj=io.BytesIO(outer.extractfile('linux-delivery-state.tar.gz').read())) as state:
                self.assertEqual({'delivery-state', 'delivery-state/' + self.receipt.name}, set(state.getnames()))
                self.assertEqual(self.receipt.read_bytes(), state.extractfile('delivery-state/' + self.receipt.name).read())
                for member in state:
                    self.assertEqual(1000, member.uid)
                    self.assertEqual(0o700 if member.isdir() else 0o600, member.mode)
        commands = self.commands()
        capture = next(command for command in commands if any('tar -C "$(dirname "$state")" -czf /backup/linux-delivery-state.tar.gz' in argument for argument in command))
        self.assertLess(commands.index(['stop', 'sxmqlive-jenkins-android-agent']), commands.index(capture))
        self.assertIn(['start', 'sxmqlive-jenkins-android-agent'], commands)

    def test_registered_missing_linux_volume_or_root_refuses_before_home_archive(self):
        node = self.home / 'nodes/linux-build/config.xml'
        node.parent.mkdir(parents=True)
        node.write_text('registered node')
        self.volumes.write_text(json.dumps([HOME_VOLUME]))
        result = self.execute('backup.sh', MISSING_AGENT_WORK='true')
        self.assertNotEqual(0, result.returncode)
        self.assertIn('delivery state volume is missing', result.stderr)
        self.assertFalse(any(command[0] in ('stop', 'start') for command in self.commands()))
        self.receipt.unlink()
        self.state.rmdir()
        result = self.execute('backup.sh')
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(any('/backup/jenkins-home.tar.gz' in command for command in self.commands()))
        self.assertFalse(list((self.root / 'backups').glob('*.tar.gz')))

    def test_real_empty_linux_and_mac_roots_are_preserved_without_fake_receipts(self):
        self.receipt.unlink()
        self.volumes.write_text(json.dumps([HOME_VOLUME]))
        result = self.execute('backup.sh', JENKINS_MAC_DELIVERY_STATE_ARCHIVE=str(self.mac_archive(empty=True)))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn('Partial migration', result.stdout)
        with tarfile.open(next((self.root / 'backups').glob('*.tar.gz'))) as outer:
            coverage = json.loads(outer.extractfile('delivery-state-coverage.json').read())
            self.assertEqual(502, coverage['macSourceUid'])
            for name in ('linux-delivery-state.tar.gz', 'mac-delivery-state.tar.gz'):
                with tarfile.open(fileobj=io.BytesIO(outer.extractfile(name).read())) as state:
                    self.assertEqual(['delivery-state'], state.getnames())

    def test_bad_source_modes_owner_links_and_unexpected_files_do_not_publish_home(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME]))
        for field in ('directory-mode', 'file-mode', 'owner', 'hardlink', 'symlink', 'unexpected'):
            with self.subTest(field=field):
                options = {}
                extra = self.state / 'unexpected'
                outside = self.root / 'outside'
                outside.write_bytes(b'fixture')
                if field == 'directory-mode':
                    self.state.chmod(0o755)
                elif field == 'file-mode':
                    self.receipt.chmod(0o644)
                elif field == 'owner':
                    options['FAKE_STATE_UID'] = '999'
                elif field == 'hardlink':
                    os.link(self.receipt, extra)
                elif field == 'symlink':
                    extra.symlink_to(outside)
                else:
                    extra.write_bytes(b'fixture')
                    extra.chmod(0o600)
                result = self.execute('backup.sh', **options)
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(list((self.root / 'backups').glob('*.tar.gz')))
                if extra.exists() or extra.is_symlink():
                    extra.unlink()
                self.state.chmod(0o700)
                self.receipt.chmod(0o600)

    def test_mac_input_must_be_absolute_private_and_without_links(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME]))
        archive = self.mac_archive()
        link = archive.parent / 'mac-link.tar.gz'
        link.symlink_to(archive)
        for value in ('relative.tar.gz', str(link)):
            with self.subTest(value=value):
                self.assertNotEqual(0, self.execute('backup.sh', JENKINS_MAC_DELIVERY_STATE_ARCHIVE=value).returncode)
        archive.chmod(0o644)
        self.assertNotEqual(0, self.execute('backup.sh', JENKINS_MAC_DELIVERY_STATE_ARCHIVE=str(archive)).returncode)
        self.assertFalse(any(command[0] in ('stop', 'start') for command in self.commands()))

    def test_complete_linux_mac_package_restores_state_and_exports_mac_without_installing(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME]))
        mac = self.mac_archive()
        self.assertEqual(0, self.execute('backup.sh', JENKINS_MAC_DELIVERY_STATE_ARCHIVE=str(mac)).returncode)
        bundle = next((self.root / 'backups').glob('*.tar.gz'))
        self.volumes.write_text('[]')
        output = self.root.resolve() / 'mac-export'
        output.mkdir(mode=0o700)
        restored = self.root / 'restored'
        result = self.execute('restore.sh', bundle, FAKE_RESTORE_ROOT=str(restored), JENKINS_MAC_DELIVERY_RESTORE_DIR=str(output))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(self.receipt.read_bytes(), (restored / AGENT_WORK_VOLUME / 'delivery-state' / self.receipt.name).read_bytes())
        self.assertEqual(mac.read_bytes(), (output / 'mac-delivery-state.tar.gz').read_bytes())
        self.assertEqual(0o600, (output / 'mac-delivery-state.tar.gz').stat().st_mode & 0o777)
        self.assertFalse(any(command[0] in ('start', 'restart') for command in self.commands()))

    def test_restore_refuses_existing_linux_work_volume_without_creating_or_overwriting(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME]))
        self.assertEqual(0, self.execute('backup.sh').returncode)
        bundle = next((self.root / 'backups').glob('*.tar.gz'))
        self.volumes.write_text(json.dumps([AGENT_WORK_VOLUME]))
        self.log.unlink()
        result = self.execute('restore.sh', bundle)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('Refusing to overwrite', result.stderr)
        self.assertFalse(any(command[:2] in (['volume', 'create'], ['volume', 'rm']) for command in self.commands()))

    def test_backup_includes_verified_private_signing_archive_and_resumes_agent(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME, SIGNING_VOLUME]))
        result = self.execute('backup.sh')
        self.assertEqual(0, result.returncode, result.stderr)
        bundle = next((self.root / 'backups').glob('*.tar.gz'))
        self.assertEqual(0o600, bundle.stat().st_mode & 0o777)
        with tarfile.open(bundle) as archive:
            self.assertEqual({'jenkins-home.tar.gz', 'ohos-signing.tar.gz', 'linux-delivery-state.tar.gz',
                              'delivery-state-coverage.json', 'controller-backup-policy.json', 'manifest.sha256'}, set(archive.getnames()))
            signing = archive.extractfile('ohos-signing.tar.gz').read()
            self.assertIn(hashlib.sha256(signing).hexdigest() + '  ohos-signing.tar.gz',
                          archive.extractfile('manifest.sha256').read().decode())
        commands = self.commands()
        self.assertIn(['stop', OHOS_AGENT], commands)
        self.assertIn(['start', OHOS_AGENT], commands)
        signing_tar = next(command for command in commands if '-czf' in command and '/backup/ohos-signing.tar.gz' in command)
        self.assertLess(commands.index(['stop', OHOS_AGENT]), commands.index(signing_tar))
        self.assertNotIn('sxmqlive_jenkins_ohos_agent_secret', json.dumps(commands))

    def test_backup_failure_still_resumes_previously_running_agent(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME, SIGNING_VOLUME]))
        result = self.execute('backup.sh', FAIL_SIGNING_TAR='true')
        self.assertNotEqual(0, result.returncode)
        self.assertIn(['start', OHOS_AGENT], self.commands())

    def test_backup_does_not_start_stopped_or_missing_agent(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME, SIGNING_VOLUME]))
        for state in ('false', 'missing'):
            with self.subTest(state=state):
                if self.log.exists():
                    self.log.unlink()
                self.assertEqual(0, self.execute('backup.sh', OHOS_RUNNING=state).returncode)
                self.assertNotIn(['stop', OHOS_AGENT], self.commands())
                self.assertNotIn(['start', OHOS_AGENT], self.commands())

    def test_home_excludes_backup_copies_and_caches_but_keeps_recovery_state(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME, 'sxmqlive_dws_state', SIGNING_VOLUME]))
        retained = ['credentials.xml', 'secrets/master.key', 'secrets/hudson.util.Secret',
                    'users/admin/config.xml', 'jobs/mobile-apps/config.xml',
                    'jobs/backups/config.xml', 'jobs/app/builds/1/build.xml',
                    'plugins/git.jpi', 'ci-auto-promotion/status.json']
        excluded = ['backups/old-home.tar.gz', 'ci-auto-promotion/backups/old/config.xml',
                    'ci-auto-promotion/backups-v2/mobile-apps__sxmqlive-android-test/' + 'a' * 40 + '/' + 'b' * 64 + '/config.xml',
                    'workspace/app/source.kt', 'caches/cache', '.cache/cache', 'war/index.html']
        for name in retained + excluded:
            target = self.home / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(name)
            target.chmod(0o600)
        result = self.execute('backup.sh')
        self.assertEqual(0, result.returncode, result.stderr)
        bundle = next((self.root / 'backups').glob('*.tar.gz'))
        with tarfile.open(bundle) as outer:
            self.assertEqual({'jenkins-home.tar.gz', 'dws-state.tar.gz', 'ohos-signing.tar.gz',
                              'linux-delivery-state.tar.gz', 'delivery-state-coverage.json', 'controller-backup-policy.json', 'manifest.sha256'}, set(outer.getnames()))
            with tarfile.open(fileobj=io.BytesIO(outer.extractfile('jenkins-home.tar.gz').read())) as home:
                names = {name[2:] if name.startswith('./') else name for name in home.getnames()}
                self.assertTrue(set(retained).issubset(names))
                self.assertTrue(set(excluded).isdisjoint(names))
                for name in retained:
                    member = home.getmember('./' + name)
                    self.assertEqual(name.encode(), home.extractfile(member).read())
                    self.assertEqual(0o600, member.mode)

    def test_retention_keeps_current_and_previous_without_deleting_unrelated_or_symlink(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME]))
        self.assertEqual(0, self.execute('backup.sh').returncode)
        output = self.root / 'backups'
        unrelated = output / 'manual-recovery.tar.gz'
        unrelated.write_bytes(b'keep')
        linked = output / 'sxmqlive-jenkins-migration-20000101-000000.tar.gz'
        linked.symlink_to(unrelated)
        for _ in range(3):
            before = {p for p in output.glob('*.tar.gz') if not p.is_symlink() and p != unrelated}
            previous = max(before, key=lambda p: p.stat().st_mtime_ns)
            result = self.execute('backup.sh')
            self.assertEqual(0, result.returncode, result.stderr)
            bundles = {p for p in output.glob('*.tar.gz') if not p.is_symlink() and p != unrelated}
            self.assertEqual(2, len(bundles))
            self.assertIn(previous, bundles)
        self.assertTrue(linked.is_symlink())
        self.assertEqual(b'keep', unrelated.read_bytes())
        self.assertFalse((output / '.backup.lock').exists())
        self.assertFalse(list(output.glob('*.partial')))

    def test_new_bundle_restores_home_dws_and_signing_into_isolated_directories(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME, 'sxmqlive_dws_state', SIGNING_VOLUME]))
        for name in ['credentials.xml', 'secrets/master.key', 'jobs/mobile-apps/config.xml']:
            file = self.home / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('fixture ' + name)
            file.chmod(0o600)
        self.assertEqual(0, self.execute('backup.sh').returncode)
        bundle = next((self.root / 'backups').glob('*.tar.gz'))
        self.volumes.write_text('[]')
        restored = self.root / 'restored'
        result = self.execute('restore.sh', bundle, FAKE_RESTORE_ROOT=str(restored))
        self.assertEqual(3, result.returncode, result.stderr)
        for file in self.home.rglob('*'):
            if file.is_file():
                recovered = restored / HOME_VOLUME / file.relative_to(self.home)
                self.assertEqual(file.read_bytes(), recovered.read_bytes())
                self.assertEqual(file.stat().st_mode & 0o777, recovered.stat().st_mode & 0o777)
        for volume in ['sxmqlive_dws_state', SIGNING_VOLUME]:
            self.assertEqual(b'{}', (restored / volume / 'state.json').read_bytes())
        self.assertEqual(self.receipt.read_bytes(), (restored / AGENT_WORK_VOLUME / 'delivery-state' / self.receipt.name).read_bytes())

    def test_corrupt_new_backup_does_not_publish_or_prune_and_resumes_controller(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME]))
        self.assertEqual(0, self.execute('backup.sh').returncode)
        before = {p.name: p.read_bytes() for p in (self.root / 'backups').glob('*.tar.gz')}
        result = self.execute('backup.sh', CORRUPT_HOME='true', CONTROLLER_RUNNING='true')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.root / 'backups').glob('*.tar.gz')})
        self.assertIn(['start', 'sxmqlive-jenkins-controller'], self.commands())
        self.assertFalse(list((self.root / 'backups').glob('*.partial')))

    def test_failed_service_resume_does_not_prune_existing_bundles(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME]))
        for _ in range(2):
            self.assertEqual(0, self.execute('backup.sh').returncode)
        before = set((self.root / 'backups').glob('*.tar.gz'))
        result = self.execute('backup.sh', CONTROLLER_RUNNING='true', FAIL_START='sxmqlive-jenkins-controller')
        self.assertNotEqual(0, result.returncode)
        self.assertTrue(all(p.exists() for p in before))

    def test_checksum_failure_does_not_publish_or_prune(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME]))
        self.assertEqual(0, self.execute('backup.sh').returncode)
        before = {p.name: p.read_bytes() for p in (self.root / 'backups').glob('*.tar.gz')}
        tar = self.root / 'tar'
        tar.write_text('''#!/usr/bin/env python3
import pathlib, subprocess, sys
args = sys.argv[1:]
subprocess.check_call(['/usr/bin/tar'] + args)
if args[0] == '-xzf' and args[-1] == 'verify':
    with pathlib.Path('verify/jenkins-home.tar.gz').open('ab') as f:
        f.write(b'changed after manifest')
''')
        tar.chmod(0o700)
        result = self.execute('backup.sh', CONTROLLER_RUNNING='true')
        self.assertNotEqual(0, result.returncode)
        self.assertIn('jenkins-home.tar.gz: FAILED', result.stdout)
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.root / 'backups').glob('*.tar.gz')})
        self.assertIn(['start', 'sxmqlive-jenkins-controller'], self.commands())

    def test_output_inside_home_or_symlink_to_home_is_rejected_before_stopping(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME]))
        linked = self.root / 'home-link'
        linked.symlink_to(self.home, target_is_directory=True)
        for output in [self.home, self.home / 'new-backups', linked / 'new-backups']:
            result = self.execute('backup.sh', JENKINS_BACKUP_DIR=str(output))
            self.assertNotEqual(0, result.returncode)
            self.assertIn('outside Jenkins Home', result.stderr)
        self.assertFalse((self.home / 'new-backups').exists())
        self.assertFalse(any(c[0] in ['stop', 'start'] for c in self.commands()))

    def test_concurrent_backup_is_rejected_without_removing_other_lock(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME]))
        lock = self.root / 'backups/.backup.lock'
        lock.mkdir(parents=True)
        result = self.execute('backup.sh')
        self.assertNotEqual(0, result.returncode)
        self.assertTrue(lock.exists())
        self.assertFalse(any(c[0] in ['stop', 'start'] for c in self.commands()))

    def test_legacy_backup_and_restore_do_not_touch_signing_volume(self):
        self.volumes.write_text(json.dumps([HOME_VOLUME]))
        self.assertEqual(0, self.execute('backup.sh').returncode)
        self.assertFalse(any(command[0] in ('stop', 'start') and command[-1] == OHOS_AGENT for command in self.commands()))
        self.log.unlink()
        self.volumes.write_text(json.dumps([SIGNING_VOLUME]))
        result = self.execute('restore.sh', self.migration(signing=False))
        self.assertEqual(3, result.returncode, result.stderr)
        self.assertFalse(any(SIGNING_VOLUME in argument for command in self.commands() for argument in command))

    def test_restore_creates_fresh_signing_volume_with_private_permissions(self):
        result = self.execute('restore.sh', self.migration())
        self.assertEqual(3, result.returncode, result.stderr)
        commands = self.commands()
        self.assertIn(['volume', 'create', SIGNING_VOLUME], commands)
        ownership = next(command for command in commands if SIGNING_VOLUME + ':/target' in command and 'chown' in command)
        self.assertEqual(['-R', '1000:1000', '/target'], ownership[-3:])
        permissions = next(command for command in commands if SIGNING_VOLUME + ':/target' in command and 'bash' in command)
        self.assertIn('chmod 700', permissions[-1])
        self.assertIn('chmod 600', permissions[-1])
        self.assertFalse(any(command[:2] == ['volume', 'rm'] for command in commands))

    def test_restore_refuses_existing_signing_volume_without_modifying_any_volume(self):
        self.volumes.write_text(json.dumps([SIGNING_VOLUME]))
        result = self.execute('restore.sh', self.migration())
        self.assertNotEqual(0, result.returncode)
        self.assertIn('Refusing to overwrite', result.stderr)
        self.assertFalse(any(command[:2] in (['volume', 'create'], ['volume', 'rm']) for command in self.commands()))

    def test_failed_signing_restore_rolls_back_only_volumes_created_this_time(self):
        result = self.execute('restore.sh', self.migration(), FAIL_SIGNING_TAR='true')
        self.assertNotEqual(0, result.returncode)
        removed = [command[-1] for command in self.commands() if command[:2] == ['volume', 'rm']]
        self.assertEqual([HOME_VOLUME, SIGNING_VOLUME], removed)
        self.assertEqual([], json.loads(self.volumes.read_text()))

    def test_failed_volume_creation_does_not_delete_uncreated_signing_volume(self):
        result = self.execute('restore.sh', self.migration(), FAIL_CREATE=SIGNING_VOLUME)
        self.assertNotEqual(0, result.returncode)
        removed = [command[-1] for command in self.commands() if command[:2] == ['volume', 'rm']]
        self.assertEqual([HOME_VOLUME], removed)

    def test_unchecksummed_signing_archive_fails_before_volume_creation(self):
        result = self.execute('restore.sh', self.migration(signing_checksum=False))
        self.assertNotEqual(0, result.returncode)
        self.assertIn('checksum manifest', result.stderr)
        self.assertFalse(any(command[:2] == ['volume', 'create'] for command in self.commands()))

    def test_unsafe_or_incomplete_manifest_fails_before_volume_creation(self):
        home_line = hashlib.sha256(b'home').hexdigest() + '  jenkins-home.tar.gz\n'
        signing_line = hashlib.sha256(b'signing').hexdigest() + '  ohos-signing.tar.gz\n'
        for manifest in (home_line + signing_line + '0' * 64 + '  ../outside\n',
                         home_line + signing_line + home_line,
                         home_line,
                         home_line + signing_line + '0' * 64 + '  /outside\n'):
            with self.subTest(manifest=manifest):
                source = self.migration()
                with tarfile.open(source) as bundle:
                    files = {item.name: bundle.extractfile(item).read() for item in bundle}
                files['manifest.sha256'] = manifest.encode('utf-8')
                with tarfile.open(source, 'w:gz') as bundle:
                    for name, content in files.items():
                        item = tarfile.TarInfo(name)
                        item.size = len(content)
                        bundle.addfile(item, io.BytesIO(content))
                result = self.execute('restore.sh', source)
                self.assertNotEqual(0, result.returncode)
                self.assertIn('checksum manifest', result.stderr)
                self.assertFalse(any(command[:2] == ['volume', 'create'] for command in self.commands()))


if __name__ == '__main__':
    unittest.main()
