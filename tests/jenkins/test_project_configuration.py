import copy
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import project_config
from jenkins import generate_jenkins_job_templates as generator
from jenkins import sync_jenkins_jobs as sync
from tests.jenkins.test_pr_validation import groovy_classpath

ROOT = Path(__file__).resolve().parents[2]


def second_project():
    value = project_config.load_project()
    value.update(id='example-mobile', brands=['example'], testOnly=True)
    for role, repo in value['repositories'].items():
        repo.update(identity='example/' + ('app' if role == 'application' else 'ci'), defaultBranch='main', trustRef='refs/heads/main')
        repo.update(ssh='git@gitee.com:' + repo['identity'] + '.git', https='https://gitee.com/' + repo['identity'] + '.git')
    value['platforms'] = {'android':{'adapter':'gradle','engines':['native'],'modules':{'native':'mobile'},
                                   'buildTypes':{'test':'Debug','release':'Release'},'applicationId':'org.example.mobile'}}
    value['jobs'] = {role:{'name':'Example Folder/' + role,'pipeline':binding['pipeline']} for role,binding in value['jobs'].items() if role in ('android_test','android_release','app_pr','ci_pr')}
    value['contract']['requiredTools'] = {'android':['affected_gradle_tests.py']}
    for key in value['credentials']: value['credentials'][key] = 'example-' + key
    for name,binding in value['providers'].items():
        binding.update(accountId='example-' + name,legacyNamespace=False)
        for key in list(binding):
            if key.endswith('CredentialId') or key=='credentialId': binding[key]='example-'+key.lower()
    value.pop('signingBackup'); value.pop('portal')
    value['backup'].update(scope='project',recipientAuthority='project-admin')
    return project_config.validate_project(value)


def mixed_second_project():
    value = second_project()
    value['scm'] = {'provider': 'gitee'}
    value['repositories']['automation'].update(provider='github', identity='example-org/mobile-ci',
        ssh='git@github.com:example-org/mobile-ci.git', https='https://github.com/example-org/mobile-ci.git')
    return project_config.validate_project(value)


class ProjectConfigurationTest(unittest.TestCase):
    def test_fixed_admin_profile_fallback_and_job_enumeration_without_dirtying_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, admin = root / 'source', root / 'admin'
            source.mkdir(); admin.mkdir()
            source_profile = second_project()
            profile = mixed_second_project(); profile.pop('testOnly')
            (admin / 'example-mobile.json').write_text(json.dumps(profile))
            original_stat, original_fstat = Path.stat, os.fstat
            def root_directory(path, *args, **kwargs):
                metadata = original_stat(path, *args, **kwargs)
                if path in (admin, *admin.parents):
                    fields = list(metadata); fields[0] &= ~0o022; fields[4] = 0
                    return os.stat_result(fields)
                return metadata
            def root_file(descriptor):
                fields = list(original_fstat(descriptor)); fields[4] = 0
                return os.stat_result(fields)
            with patch.object(project_config, 'PROJECTS_DIR', source), patch.object(project_config, 'ADMIN_PROJECTS_DIR', admin), \
                    patch.object(Path, 'stat', root_directory), patch.object(os, 'fstat', root_file), \
                    patch.dict(os.environ, CI_PROJECT_CONFIG=str(root / 'candidate.json')):
                self.assertEqual(profile, project_config.load_project('example-mobile'))
                self.assertEqual(['example-mobile'], project_config.available_project_ids())
                self.assertEqual(profile, project_config.profile_for_job('Example Folder/android_test', 'android_test'))
                self.assertEqual([], list(source.iterdir()))
                other = copy.deepcopy(profile); other['id'] = 'another-mobile'
                (source / 'another-mobile.json').write_text(json.dumps(other))
                self.assertEqual(['another-mobile', 'example-mobile'], project_config.available_project_ids())
                with self.assertRaises(project_config.ProjectConfigError):
                    project_config.profile_for_job('Example Folder/android_test', 'android_test')
                (source / 'example-mobile.json').write_text(json.dumps(source_profile))
                with patch.object(project_config, '_admin_projects_directory', side_effect=AssertionError('source profile must take priority')):
                    self.assertEqual(source_profile, project_config.load_project('example-mobile'))

    def test_admin_profile_rejects_non_root_writeable_symlink_hardlink_oversize_and_test_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, admin = root / 'source', root / 'admin'
            source.mkdir(); admin.mkdir()
            target = admin / 'example-mobile.json'
            profile = mixed_second_project(); profile.pop('testOnly')
            data = json.dumps(profile)
            target.write_text(data); target.chmod(0o640)
            original_stat, original_fstat = Path.stat, os.fstat
            invalid_directory, owner = {}, [0]
            def root_directory(path, *args, **kwargs):
                metadata = original_stat(path, *args, **kwargs)
                if path in (admin, *admin.parents):
                    fields = list(metadata); fields[0] &= ~0o022; fields[4] = 0
                    for index, value in invalid_directory.get(path, {}).items(): fields[index] = value
                    return os.stat_result(fields)
                return metadata
            def root_file(descriptor):
                fields = list(original_fstat(descriptor)); fields[4] = owner[0]
                return os.stat_result(fields)
            with patch.object(project_config, 'PROJECTS_DIR', source), patch.object(project_config, 'ADMIN_PROJECTS_DIR', admin), \
                    patch.object(Path, 'stat', root_directory), patch.object(os, 'fstat', root_file):
                for path, fields in ((admin, {4: 1234}), (admin.parent, {4: 1234}),
                                     (admin, {0: stat.S_IFDIR | 0o770}), (admin.parent, {0: stat.S_IFDIR | 0o777})):
                    invalid_directory[path] = fields
                    with self.subTest(path=path, fields=fields), self.assertRaises(project_config.ProjectConfigError):
                        project_config.load_project('example-mobile')
                    with self.assertRaises(project_config.ProjectConfigError): project_config.available_project_ids()
                    invalid_directory.clear()
                owner[0] = 1234
                with self.assertRaises(project_config.ProjectConfigError): project_config.load_project('example-mobile')
                owner[0] = 0
                target.chmod(0o660)
                with self.assertRaises(project_config.ProjectConfigError): project_config.load_project('example-mobile')
                target.chmod(0o640)
                link = admin / 'linked'; os.link(target, link)
                with self.assertRaises(project_config.ProjectConfigError): project_config.load_project('example-mobile')
                link.unlink()
                target.write_text(' ' * 65537)
                with self.assertRaises(project_config.ProjectConfigError): project_config.load_project('example-mobile')
                target.write_text(json.dumps(dict(profile, testOnly=True)))
                with self.assertRaises(project_config.ProjectConfigError): project_config.load_project('example-mobile')
                target.write_text(data); target.rename(link); target.symlink_to(link)
                with self.assertRaises(project_config.ProjectConfigError): project_config.load_project('example-mobile')
                target.unlink(); link.rename(target)
                alias = root / 'alias'; alias.symlink_to(admin, target_is_directory=True)
                with patch.object(project_config, 'ADMIN_PROJECTS_DIR', alias), self.assertRaises(project_config.ProjectConfigError):
                    project_config.load_project('example-mobile')
                with patch.object(project_config, 'ADMIN_PROJECTS_DIR', root / 'missing'), self.assertRaises(FileNotFoundError):
                    project_config.load_project('example-mobile')

    def test_mixed_repository_providers_keep_private_application_and_render_github_tools(self):
        original = second_project()
        self.assertEqual(original, project_config.validate_project(original))
        self.assertNotIn('provider', original['repositories']['automation'])
        profile = mixed_second_project()
        self.assertEqual(original['repositories']['application'], profile['repositories']['application'])
        values = project_config.project_template_values(profile, 'app_pr')
        self.assertEqual('git@gitee.com:example/app.git', values['APP_SSH'])
        self.assertEqual('git@github.com:example-org/mobile-ci.git', values['CI_SSH'])
        self.assertEqual('main', values['ROLE_BRANCH'])
        self.assertEqual('example', values['ROLE_NAMESPACE'])
        self.assertEqual('https://github.com/example-org/mobile-ci.git', values['CI_CHECKOUT_URL'])
        self.assertEqual('', values['CI_CHECKOUT_CREDENTIAL'])
        self.assertEqual('hudson.plugins.git.browser.GithubWeb', values['CI_BROWSER_CLASS'])
        with self.assertRaises(project_config.ProjectConfigError):
            project_config.project_template_values(profile, 'ci_pr')
        with patch.dict(os.environ, CI_AUTOMATION_COMMIT='a' * 40):
            configs = sync.render_all(profile=profile)
        self.assertEqual(2, len(configs))
        for text in configs.values():
            xml = ET.fromstring(text)
            remote = xml.find('.//scm/userRemoteConfigs/hudson.plugins.git.UserRemoteConfig')
            self.assertEqual('https://github.com/example-org/mobile-ci.git', remote.findtext('url'))
            self.assertEqual('', remote.findtext('credentialsId'))
            script = xml.findtext('.//org.biouno.unochoice.CascadeChoiceParameter/script/secureScript/script')
            self.assertIn('git@gitee.com:example/app.git', script)

    def test_mixed_repository_provider_rejects_wrong_host_identity_ref_and_application_override(self):
        original = mixed_second_project()
        cases = []
        for role, fields in (
                ('application', {'provider': 'github'}), ('application', {'provider': None}),
                ('automation', {'provider': 'other'}), ('automation', {'provider': ['github']}),
                ('automation', {'provider': {'provider': 'github'}}), ('automation', {'provider': 'gitee'}),
                ('automation', {'identity': 'example.org/mobile-ci'}), ('automation', {'identity': 'example-org/..'}),
                ('automation', {'ssh': 'git@gitee.com:example-org/mobile-ci.git'}),
                ('automation', {'https': 'https://github.com.evil/example-org/mobile-ci.git'}),
                ('automation', {'https': 'https://user@github.com/example-org/mobile-ci.git'}),
                ('automation', {'defaultBranch': 'main..other'}),
                ('automation', {'trustRef': 'refs/heads/master'}), ('automation', {'path': '/candidate/profile'})):
            bad = copy.deepcopy(original)
            bad['repositories'][role].update(fields)
            cases.append(bad)
        for bad in cases:
            with self.subTest(repositories=bad['repositories']), self.assertRaises(project_config.ProjectConfigError):
                project_config.validate_project(bad)

    def test_all_public_tool_checkouts_use_https_without_gitee_credentials(self):
        profile = project_config.load_project()
        profile['repositories']['automation'].update(provider='github', identity='example-org/mobile-ci',
            ssh='git@github.com:example-org/mobile-ci.git', https='https://github.com/example-org/mobile-ci.git')
        url = profile['repositories']['automation']['https']
        checked = 0
        for path in (ROOT / 'jenkins/templates/local').rglob('*.xml.template'):
            xml = ET.fromstring(project_config.render_project_text(path.read_text(), profile))
            remote = xml.find('definition/scm/userRemoteConfigs/hudson.plugins.git.UserRemoteConfig')
            if remote is None:
                continue
            checked += 1
            with self.subTest(template=path.name):
                self.assertEqual(url, remote.findtext('url'))
                self.assertEqual('', remote.findtext('credentialsId'))
                browser = xml.find('definition/scm/browser')
                if browser is not None:
                    self.assertEqual('hudson.plugins.git.browser.GithubWeb', browser.get('class'))
        self.assertEqual(9, checked)
        for name in ('Jenkinsfile.pr-validation', 'Jenkinsfile.project-pr-validation',
                     'Jenkinsfile.signing.maintenance', 'Jenkinsfile.signing.backup'):
            text = project_config.render_project_text((ROOT / 'jenkins/templates/pipelines' / name).read_text(), profile, 'app_pr')
            with self.subTest(pipeline=name):
                self.assertNotIn(profile['repositories']['automation']['ssh'], text)
                remotes = re.findall(r"userRemoteConfigs: \[\[credentialsId: '([^']*)',\s*url: (env.CI_REPOSITORY_URL|'[^']*')", text)
                self.assertTrue(remotes)
                for credential, remote in remotes:
                    if remote in ('env.CI_REPOSITORY_URL', "'" + url + "'"):
                        self.assertEqual('', credential)
                    else:
                        self.assertEqual("'" + profile['repositories']['application']['ssh'] + "'", remote)
                        self.assertEqual(profile['credentials']['scm'], credential)

    def test_github_actions_profile_cannot_use_gitee_tools_even_with_matching_urls(self):
        value = second_project()
        value['scm'] = {'provider': 'github', 'allowForks': True}
        for binding in value['repositories'].values():
            binding.update(provider='github', ssh=binding['ssh'].replace('gitee.com', 'github.com'),
                           https=binding['https'].replace('gitee.com', 'github.com'))
        for key in ('credentials', 'providers', 'jobs'):
            value[key] = {}
        for key in ('runtime', 'backup'):
            value.pop(key)
        self.assertEqual(value, project_config.validate_project(value))
        value['repositories']['automation'].update(provider='gitee', ssh='git@gitee.com:example/ci.git',
                                                   https='https://gitee.com/example/ci.git')
        with self.assertRaises(project_config.ProjectConfigError):
            project_config.validate_project(value)

    def test_default_backup_recipient_matches_approved_application_public_key(self):
        source = os.environ.get('APP_SOURCE_ROOT')
        if not source:
            self.skipTest('需要 APP_SOURCE_ROOT 中已审应用公钥；不下载或生成接收人')
        profile = project_config.load_project('sxmqlive')
        backup = profile['signingBackup']
        public = Path(source) / backup['recipientPath']
        result = subprocess.run(['openssl', 'pkey', '-pubin', '-outform', 'DER'],
                                input=public.read_bytes(), capture_output=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', errors='replace'))
        recipient = hashlib.sha256(result.stdout).hexdigest()
        self.assertEqual(recipient, backup['recipientSha256'])
        self.assertEqual(recipient, backup['legacyRecipientSha256'])
        changed = copy.deepcopy(profile)
        changed['signingBackup']['recipientSha256'] = 'f' * 64
        with self.assertRaises(project_config.ProjectConfigError):
            project_config.validate_project(changed)

    def test_schema_rejects_untrusted_paths_commands_accounts_and_platform_jobs(self):
        original=project_config.load_project(); before=copy.deepcopy(original)
        self.assertEqual(original,project_config.validate_project(original)); self.assertEqual(before,original)
        cases=[]
        for key,val in [('id','../other'),('version',True),('hook','curl evil')]:
            bad=copy.deepcopy(original); bad[key]=val; cases.append(bad)
        for field,val in [('artifact','../outside.apk'),('artifact','a//b.apk'),('artifact','{brand.__class__}.apk'),('modules',{'cmp':'$(id)','kuikly':'androidKuiklyApp'})]:
            bad=copy.deepcopy(original);bad['platforms']['android'][field]=val;cases.append(bad)
        bad=copy.deepcopy(original);bad['providers']['pgyer'].update(accountId='other-account',legacyNamespace=True);cases.append(bad)
        bad=second_project();bad['providers']['pgyer']['legacyNamespace']=True;cases.append(bad)
        bad=copy.deepcopy(original);del bad['platforms']['ios'];cases.append(bad)
        bad=copy.deepcopy(original);bad['jobs']['app_pr']['pipeline']='evil.groovy';cases.append(bad)
        bad=copy.deepcopy(original);bad['credentials']['scm']='$(cat secret)';cases.append(bad)
        for bad in cases:
            with self.subTest(bad=bad.get('id')):
                with self.assertRaises(project_config.ProjectConfigError):project_config.validate_project(bad)

    def test_profile_cli_emits_utf8_with_ascii_process_locale(self):
        result=subprocess.run(['python3',str(ROOT/'project_config.py'),'--project','sxmqlive'],
                              env=dict(os.environ,PYTHONIOENCODING='ascii',LC_ALL='C'),capture_output=True,timeout=30)
        self.assertEqual(0,result.returncode,result.stderr.decode('utf-8',errors='replace'))
        value=json.loads(result.stdout.decode('utf-8'))
        self.assertEqual('运维/sxmqlive-signing-backup',value['jobs']['signing_backup']['name'])

    def test_fixed_loader_job_identity_and_symlink_writeability_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();target=root/'example-mobile.json';target.write_text(json.dumps(second_project()),encoding='utf-8')
            with patch.object(project_config,'PROJECTS_DIR',root):
                self.assertEqual('example-mobile',project_config.profile_for_job('Example Folder/android_test','android_test')['id'])
                for job in ('evil/job','Example Folder/ci_pr'):
                    with self.assertRaises(project_config.ProjectConfigError):project_config.profile_for_job(job,'android_test')
                with self.assertRaises(project_config.ProjectConfigError):project_config.load_project('../example-mobile')
                target.chmod(0o666)
                with self.assertRaises(project_config.ProjectConfigError):project_config.load_project('example-mobile')
                target.chmod(0o600);other=root/'other';target.rename(other);target.symlink_to(other)
                with self.assertRaises(project_config.ProjectConfigError):project_config.load_project('example-mobile')

    def test_android_only_main_renamed_jobs_render_fixed_selectors_and_isolated_approval(self):
        profile=second_project()
        with patch.dict(os.environ,CI_AUTOMATION_COMMIT='a'*40): configs=sync.render_all(profile=profile)
        self.assertEqual(2,len(configs))
        for spec,text in configs.items():
            xml=ET.fromstring(text);script=xml.findtext('.//org.biouno.unochoice.CascadeChoiceParameter/script/secureScript/script')
            self.assertIn("def jobName = '"+spec.full_name+"'",script)
            self.assertIn('JobWrapperFactory.createJobWrapper(job)',script);self.assertIn('job.checkPermission(hudson.model.Item.BUILD)',script)
            self.assertIn('git@gitee.com:example/app.git',script);self.assertIn("'example-scm'",script)
            self.assertNotIn('sxmqlive-android',script)
            parameters={node.findtext('name'):node for node in xml.find('.//parameterDefinitions')}
            self.assertEqual(['example'],[node.text for node in parameters['BRAND'].findall('choices/a/string')])
            self.assertEqual(['native'],[node.text for node in parameters['UI_ENGINE'].findall('choices/a/string')])
            if spec.script_path.endswith('.test'):
                self.assertIn("it == 'main'",script)
                self.assertEqual(['default'],[node.text for node in parameters['BRANCH_TYPE'].findall('choices/a/string')])
        before={path:path.read_bytes() for path in generator.TEMPLATE_DIR.glob('*-job.xml.template')}
        approval_before=generator.APPROVAL_FILE.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)/'out'
            self.assertEqual(0,generator.synchronize(Path(directory)/'not-candidate-config.json',False,profile,out))
            self.assertEqual(0,generator.synchronize(Path(directory)/'not-candidate-config.json',True,profile,out))
            self.assertEqual(0,generator.synchronize(Path(directory)/'not-candidate-config.json',True,profile))
            approval=(out/generator.APPROVAL_FILE.name).read_text()
            for spec in sync.project_job_specs(profile):
                text=(out/spec.template_name).read_text().replace('__JOB_NAME__',spec.full_name)
                script=ET.fromstring(text).findtext('.//org.biouno.unochoice.CascadeChoiceParameter/script/secureScript/script')
                digest='SHA512:'+hashlib.sha512(('groovy:'+script).encode()).hexdigest()
                self.assertIn(digest,approval)
                self.assertNotIn('approveAll',approval)
        self.assertEqual(approval_before,generator.APPROVAL_FILE.read_bytes())
        for path,data in before.items():self.assertEqual(data,path.read_bytes())

    def test_actual_ios_config_commands_use_trusted_facts_over_candidate_credentials(self):
        app = Path(os.environ['APP_SOURCE_ROOT'])
        document = json.loads((app/'ci/brands.json').read_text(encoding='utf-8'))
        brand = 'kangmin'
        team = document[brand]['ios']['signing']['test']['team']
        namespace = document['_iosTeams'][team]['credentialNamespace']
        document[brand]['ios']['signing']['release']['enabled'] = True
        candidate = copy.deepcopy(document)
        for team_config in candidate['_iosTeams'].values(): team_config['credentialNamespace'] = 'candidate-other-team'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); source = root/'source'; trusted = root/'trusted'
            (source/'ci').mkdir(parents=True); (trusted/'metadata').mkdir(parents=True)
            (source/'ci/brands.json').write_text(json.dumps(candidate),encoding='utf-8')
            facts = trusted/'metadata/approved-brands.json'; facts.write_text(json.dumps(document),encoding='utf-8')
            environment = dict(os.environ, APP_SOURCE_ROOT=str(source), CI_TOOL_ROOT=str(ROOT),
                               CI_SIGNING_CONFIG_ROOT=str(trusted), CI_BRANDS_FILE='metadata/approved-brands.json',
                               CI_BRAND=brand, CI_UI_ENGINE_ARGS='', CI_TEST_CHANNEL='pgyer', CI_PROJECT_ID='sxmqlive', JOB_NAME='')
            for name in ('ios.test','ios.release'):
                pipeline=(ROOT/'jenkins'/('Jenkinsfile.'+name)).read_text()
                command = re.search(r"script: '(python3 .*ios_release.py\" config[^']*)'",pipeline).group(1)
                result=subprocess.run(['bash','-eu','-c',command],cwd=source,env=environment,capture_output=True,text=True,timeout=30)
                self.assertEqual(0,result.returncode,result.stderr)
                config=json.loads(result.stdout)
                team = document[brand]['ios']['signing']['test' if name.endswith('test') else 'release']['team']
                expected_namespace = document['_iosTeams'][team]['credentialNamespace']
                self.assertEqual(expected_namespace+'-distribution-p12',config['distribution-p12-credential'])
                self.assertNotIn('candidate-other-team',json.dumps(config))
                self.assertIn('branches: [[name: env.CI_TRUSTED_FACTS_REF]]',pipeline)
                self.assertIn('env.IOS_BRAND_CONFIG =',pipeline)
                for command_name in ('resolve-brand','config','validate','prepare','notify','record-app-store'):
                    lines=[line for line in pipeline.splitlines() if 'ios_release.py" '+command_name in line]
                    self.assertTrue(lines,command_name)
                    for line in lines:self.assertIn('--config="$CI_SIGNING_CONFIG_ROOT/$CI_BRANDS_FILE"',line)

    def test_groovy_loader_and_python_compile_the_same_fixed_project_gate(self):
        classpath=groovy_classpath();self.assertTrue(classpath)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();root.chmod(0o700)
            original=project_config.load_project();profile=mixed_second_project();profile.pop('testOnly')
            profile['scm'] = {'provider': 'gitee'}
            for value in (original,profile):
                path=root/(value['id']+'.json');path.write_text(json.dumps(value),encoding='utf-8');path.chmod(0o600)
            source=(ROOT/'jenkins/templates/pipelines/Jenkinsfile.project-pr-validation').read_text()
            expected=root/'expected.txt';expected.write_text(project_config.render_project_text(source,profile,'app_pr'),encoding='utf-8')
            script=root/'check.groovy';script.write_text("""def policy = new GroovyClassLoader().parseClass(new File(args[0]))
def directory = new File(args[1])
if (args.length > 4) {
    try { policy.loadProject(directory,'example-mobile'); throw new AssertionError('invalid profile accepted') }
    catch (IllegalArgumentException expected) { println('invalid profile rejected: ' + expected.message); return }
}
def original = policy.loadProject(directory,'sxmqlive')
def profile = policy.loadProject(directory,'example-mobile')
assert policy.jobMap(profile).size() == 4
assert policy.renderProjectText(new File(args[2]).getText('UTF-8'),profile,original,'app_pr') == new File(args[3]).getText('UTF-8')
println('Groovy trusted project and compiled proof text match')
""",encoding='utf-8')
            result=subprocess.run(['java','-cp',classpath,'groovy.ui.GroovyMain',str(script),str(ROOT/'jenkins/controller/CiAutoPromotion.groovy'),str(root),str(ROOT/'jenkins/templates/pipelines/Jenkinsfile.project-pr-validation'),str(expected)],capture_output=True,text=True,timeout=30)
            self.assertEqual(0,result.returncode,result.stderr+result.stdout)
            self.assertIn('compiled proof text match',result.stdout)
            example=copy.deepcopy(profile);example['testOnly']=True
            self.assertTrue(project_config.validate_project(example)['testOnly'])
            (root/'example-mobile.json').write_text(json.dumps(example),encoding='utf-8')
            result=subprocess.run(['java','-cp',classpath,'groovy.ui.GroovyMain',str(script),str(ROOT/'jenkins/controller/CiAutoPromotion.groovy'),str(root),str(ROOT/'jenkins/Jenkinsfile.ci-pr-validation'),str(expected),'reject'],capture_output=True,text=True,timeout=30)
            self.assertEqual(0,result.returncode,result.stderr+result.stdout)
            self.assertIn('testOnly 示例不能安装为 Controller 受信项目',result.stdout)
            bad_profiles=[]
            for binding in ({'provider':'github'}, {'provider':'gitee','allowForks':True}, {'provider':'other'}):
                bad=copy.deepcopy(profile);bad['scm']=binding;bad_profiles.append(bad)
            for role, fields in (('application', {'provider':'github'}), ('automation', {'provider':'gitee'}),
                                 ('automation', {'provider':'other'}), ('automation', {'identity':'example.org/mobile-ci'}),
                                 ('automation', {'https':'https://github.com.evil/example-org/mobile-ci.git'}),
                                 ('automation', {'trustRef':'refs/heads/master'})):
                bad=copy.deepcopy(profile);bad['repositories'][role].update(fields);bad_profiles.append(bad)
            bad=copy.deepcopy(profile);bad['platforms']['android'].update(engines=['cmp'],modules={'cmp':'mobile'});bad_profiles.append(bad)
            bad=copy.deepcopy(profile);bad['platforms']['android']['modules']={'native':'mobile','cmp':'other'};bad_profiles.append(bad)
            bad=copy.deepcopy(profile);bad['platforms']['android']['artifact']='{brand.__class__}.apk';bad_profiles.append(bad)
            bad=copy.deepcopy(profile);bad['platforms']['android']['artifact']='a//b.apk';bad_profiles.append(bad)
            bad=copy.deepcopy(profile);bad['platforms']['ios']={'adapter':'xcode','engines':['native'],'workspace':'A.xcworkspace','project':'A.xcodeproj'}
            bad['jobs'].update(ios_test={'name':'example/ios-test','pipeline':'jenkins/Jenkinsfile.ios.test'},ios_release={'name':'example/ios-release','pipeline':'jenkins/Jenkinsfile.ios.release'});bad_profiles.append(bad)
            for bad in bad_profiles:
                with self.assertRaises(project_config.ProjectConfigError): project_config.validate_project(bad)
                (root/'example-mobile.json').write_text(json.dumps(bad),encoding='utf-8')
                result=subprocess.run(['java','-cp',classpath,'groovy.ui.GroovyMain',str(script),str(ROOT/'jenkins/controller/CiAutoPromotion.groovy'),str(root),str(ROOT/'jenkins/Jenkinsfile.ci-pr-validation'),str(expected),'reject'],capture_output=True,text=True,timeout=30)
                self.assertEqual(0,result.returncode,result.stderr+result.stdout)
                self.assertIn('invalid profile rejected',result.stdout)


    def test_actual_groovy_pipeline_modes_reject_old_form_and_skip_quiet_notifications(self):
        classpath=groovy_classpath();self.assertTrue(classpath and shutil.which('java'))
        paths=[ROOT/'jenkins'/('Jenkinsfile.'+name) for name in ('test','release','ios.test','ios.release','ohos.test')]
        for path in paths:
            text=path.read_text();self.assertLess(text.index("stage('Validate Delivery Mode')"),text.index('withCredentials'))
        result=subprocess.run(['java','-cp',classpath,'groovy.ui.GroovyMain',str(ROOT/'tests/jenkins/fixtures/delivery_mode_runtime.groovy')]+[str(path) for path in paths],capture_output=True,text=True,timeout=30)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertIn('Actual packaging modes passed',result.stdout)


if __name__=='__main__':unittest.main()
