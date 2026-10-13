from unittest.mock import patch
import os
import json
import hashlib
import re
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from jenkins import generate_jenkins_job_templates as templates
from jenkins import sync_jenkins_jobs as jobs


class GenerateJenkinsJobTemplatesTest(unittest.TestCase):
    def setUp(self):
        patched = patch.dict(os.environ, CI_AUTOMATION_COMMIT="a" * 40)
        patched.start()
        self.addCleanup(patched.stop)

    def fixture_files(self, directory):
        root = Path(directory)
        for spec in jobs.JOB_SPECS:
            (root / spec.template_name).write_text((templates.TEMPLATE_DIR / spec.template_name).read_text())
        approval = root / 'approvals.groovy'
        approval.write_text(templates.APPROVAL_FILE.read_text())
        config = root / 'brands.json'
        brands = {}
        for spec in jobs.JOB_SPECS:
            tree = ET.fromstring((root / spec.template_name).read_text())
            choice = next(node for node in tree.findall('.//hudson.model.ChoiceParameterDefinition') if node.findtext('name') == 'BRAND')
            for brand in choice.findall('choices/a/string'):
                value = brands.setdefault(brand.text, {})
                if spec.platform == 'ios': value['ios'] = {'signing': {'test': {'enabled': True}, 'release': {'enabled': True}}}
                if spec.platform == 'harmony': value['harmony'] = {'signing': {'test': {'enabled': True}, 'release': {'enabled': True}}}
        config.write_text(json.dumps(brands))
        return root, approval, config

    def test_canonical_selectors_regenerate_existing_xml_byte_for_byte(self):
        for spec in jobs.JOB_SPECS:
            with self.subTest(job=spec.name):
                source = (templates.TEMPLATE_DIR / spec.template_name).read_text()
                root = ET.fromstring(source)
                brand = next(node for node in root.findall('.//hudson.model.ChoiceParameterDefinition')
                             if node.findtext('name') == 'BRAND')
                brands = [node.text for node in brand.findall('choices/a/string')]
                self.assertEqual(source, templates.render_template(source, brands, spec))

    def test_xml_security_drift_is_rejected_even_with_matching_modified_hashes(self):
        for spec in jobs.JOB_SPECS:
            with self.subTest(job=spec.name), tempfile.TemporaryDirectory() as directory:
                root, approval, config = self.fixture_files(directory)
                with patch.multiple(templates, TEMPLATE_DIR=root, APPROVAL_FILE=approval, REPO_ROOT=root):
                    self.assertEqual(0, templates.synchronize(config, check=True))
                    path = root / spec.template_name
                    source = path.read_text()
                    guard = 'job.checkPermission(hudson.model.Item.BUILD)\n'
                    self.assertIn(guard, source)
                    path.write_text(source.replace(guard, ''))
                    hashes = []
                    for job, xml in jobs.render_all(root).items():
                        script = ET.fromstring(xml).findtext('.//secureScript/script')
                        hashes.append('SHA512:' + hashlib.sha512(('groovy:' + script).encode()).hexdigest())
                    block = '// BEGIN MANAGED SELECTOR HASHES\n[\n'
                    block += ''.join("    '{}',\n".format(value) for value in hashes)
                    block += '].each { approval.approveScript(it) }\n// END MANAGED SELECTOR HASHES'
                    approval.write_text(re.sub(r'// BEGIN MANAGED SELECTOR HASHES\n.*?// END MANAGED SELECTOR HASHES',
                                               lambda match: block, approval.read_text(), flags=re.DOTALL))
                    modified = path.read_text(), approval.read_text()
                    self.assertEqual(1, templates.synchronize(config, check=True))
                    self.assertEqual(modified, (path.read_text(), approval.read_text()))
                    self.assertEqual(0, templates.synchronize(config, check=False))
                    self.assertEqual(source, path.read_text())
                    self.assertEqual(0, templates.synchronize(config, check=True))

    def test_generator_uses_job_spec_platform_and_template_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root, approval, config = self.fixture_files(directory)
            config = root / 'brands.json'
            config.write_text(json.dumps({'first': {'ios': {}, 'harmony': {}}, 'second': {'ios': {}}}))
            original = jobs.JOB_SPECS[-1]
            spec = original._replace(template_name='renamed-job.xml.template', platform='ios')
            (root / spec.template_name).write_text((root / original.template_name).read_text())
            with patch.multiple(templates, TEMPLATE_DIR=root, APPROVAL_FILE=approval, REPO_ROOT=root, JOB_SPECS=(spec,)):
                self.assertEqual(0, templates.synchronize(config, check=False))
                self.assertEqual(0, templates.synchronize(config, check=True))
            xml = ET.fromstring((root / spec.template_name).read_text())
            selector = xml.findtext('.//secureScript/script')
            self.assertIn("def allowedBrands = ['first', 'second']", selector)
            self.assertIn('"${brand}-ios-v"', selector)

    def test_harmony_whitelist_requires_own_platform_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'brands.json'
            config.write_text(json.dumps({'_comment': 'ignored',
                                          'first': {'ios': {}, 'harmony': {'signing': {'enabled': False}}},
                                          'second': {'ios': {}}, 'third': {'harmony': []}}))
            brands = templates.load_platform_brands(config)
        self.assertEqual(['first', 'second', 'third'], brands['android'])
        self.assertEqual(['first', 'second'], brands['ios'])
        # 未验收的品牌仍可选择，由构建 preflight 的 enabled 门禁阻断。
        self.assertEqual(['first'], brands['harmony'])
        self.assertEqual(['android', 'android', 'ios', 'ios', 'harmony', 'harmony'],
                         [spec.platform for spec in jobs.JOB_SPECS])

    def test_harmony_render_updates_choices_and_selector_without_changing_branch_types(self):
        spec = next(spec for spec in jobs.JOB_SPECS if spec.name == 'sxmqlive-ohos-test')
        filename = spec.template_name
        source = (templates.TEMPLATE_DIR / filename).read_text()
        rendered = templates.render_template(source, ['first', 'second'], spec)
        root = ET.fromstring(rendered)
        parameters = {node.findtext('name'): node for node in root.find('.//parameterDefinitions')}
        self.assertEqual(['first', 'second'], [node.text for node in parameters['BRAND'].findall('choices/a/string')])
        self.assertEqual(['feature', 'dev', 'release'], [node.text for node in parameters['BRANCH_TYPE'].findall('choices/a/string')])
        script = parameters['GIT_BRANCH'].findtext('script/secureScript/script')
        self.assertIn("def allowedBrands = ['first', 'second']\ndef brand = 'first'", script)
        self.assertEqual(3, rendered.count('__JOB_NAME__'))
        self.assertEqual('__CI_AUTOMATION_COMMIT__', root.findtext('definition/scm/branches/hudson.plugins.git.BranchSpec/name'))
        self.assertEqual(rendered, templates.render_template(rendered, ['first', 'second'], spec))

    def test_harmony_formal_render_updates_brands_and_keeps_release_tag_selector(self):
        spec = next(spec for spec in jobs.JOB_SPECS if spec.name == 'sxmqlive-ohos-formal-release')
        filename = spec.template_name
        source = (templates.TEMPLATE_DIR / filename).read_text()
        rendered = templates.render_template(source, ['first', 'second'], spec)
        root = ET.fromstring(rendered)
        parameters = {node.findtext('name'): node for node in root.find('.//parameterDefinitions')}
        self.assertEqual(['first', 'second'], [node.text for node in parameters['BRAND'].findall('choices/a/string')])
        self.assertEqual(['Release'], [node.text for node in parameters['BUILD_TYPE'].findall('choices/a/string')])
        script = parameters['GIT_TAG'].findtext('script/secureScript/script')
        self.assertIn("def allowedBrands = ['first', 'second']\ndef brand = 'first'", script)
        self.assertIn('"${brand}-ohos-v"', script)
        self.assertEqual(3, rendered.count('__JOB_NAME__'))
        self.assertEqual(rendered, templates.render_template(rendered, ['first', 'second'], spec))

    def test_generated_whitelist_keeps_folder_scoped_job_binding(self):
        for spec in jobs.JOB_SPECS:
            with self.subTest(template=spec.template_name):
                source = (templates.TEMPLATE_DIR / spec.template_name).read_text()
                generated = templates.render_template(source, ['first', 'second'], spec)
                root = ET.fromstring(jobs.render_job_config(generated, spec))
                self.assertEqual('mobile-apps/' + spec.name, root.findtext('.//projectFullName'))
                self.assertEqual(spec.name, root.findtext('.//projectName'))
                self.assertIn("def jobName = 'mobile-apps/" + spec.name + "'", root.findtext('.//secureScript/script'))

    def test_empty_platform_whitelist_fails_explicitly(self):
        with self.assertRaisesRegex(templates.TemplateGenerationError, '没有已登记的平台品牌'):
            templates.render_template('', [], jobs.JOB_SPECS[-2])

    def test_hash_drift_blocks_check_and_generation_updates_only_reviewed_templates(self):
        with tempfile.TemporaryDirectory() as directory:
            root, approval, config = self.fixture_files(directory)
            original = templates.APPROVAL_FILE.read_text()
            prefix, managed = original.split('// BEGIN MANAGED SELECTOR HASHES\n')
            approval.write_text(prefix + '// BEGIN MANAGED SELECTOR HASHES\n' + managed.replace("'SHA512:", "'SHA512:0"))
            stale = approval.read_text()
            with patch.object(templates, 'TEMPLATE_DIR', root), patch.object(templates, 'APPROVAL_FILE', approval), \
                    patch.object(templates, 'REPO_ROOT', root):
                self.assertEqual(1, templates.synchronize(config, check=True))
                self.assertEqual(stale, approval.read_text())
                self.assertEqual(0, templates.synchronize(config, check=False))
                self.assertEqual(0, templates.synchronize(config, check=True))
            self.assertEqual(prefix, approval.read_text().split('// BEGIN MANAGED SELECTOR HASHES\n')[0])
            managed = approval.read_text().split('// BEGIN MANAGED SELECTOR HASHES\n')[1].split('// END MANAGED SELECTOR HASHES')[0]
            expected = set()
            for spec, source in jobs.render_all(root).items():
                script = ET.fromstring(source).findtext('.//org.biouno.unochoice.CascadeChoiceParameter/script/secureScript/script')
                expected.add('SHA512:' + hashlib.sha512(('groovy:' + script).encode()).hexdigest())
            self.assertEqual(expected, set(re.findall(r'SHA512:[0-9a-f]{128}', managed)))


if __name__ == '__main__':
    unittest.main()
