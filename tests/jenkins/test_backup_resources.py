import copy
import unittest

from jenkins.remote.backup_resources import resources
from project_config import load_project


def compose_fixture(project):
    roles = project['runtime']['roles']
    config = {'name': project['id'], 'services': {}, 'volumes': {}}
    for role, service in roles.items():
        config['services'][service] = {'container_name': project['id'] + '-' + role,
            'image': 'fixture/controller:fixed', 'environment': {}, 'volumes': []}
    work = '/home/jenkins/agent'
    config['services'][roles['android']]['environment'] = {
        'JENKINS_AGENT_WORKDIR': work, 'DELIVERY_STATE_ROOT': work + '/delivery-state'}
    for role, target, name in (('controller', '/var/jenkins_home', 'home'),
                              ('android', '/home/jenkins/.dws-state', 'dws'),
                              ('android', work, 'work'),
                              ('maintenance', project['signingBackup']['root'], 'signing')):
        config['volumes'][name] = {'name': project['id'] + '-' + name}
        config['services'][roles[role]]['volumes'].append({'type': 'volume', 'source': name, 'target': target})
    return config


class BackupResourcesTest(unittest.TestCase):
    def test_project_admin_scope_cannot_collect_controller_disaster_recovery(self):
        project = load_project()
        project['backup'].update(scope='project', recipientAuthority='project-admin')
        config = compose_fixture(project)
        with self.assertRaisesRegex(ValueError, 'controller-admin'):
            resources(config, project)
        self.assertEqual('sxmqlive-maintenance', resources(config, project, controller_dr=False)['maintenance'])

    def test_role_mapping_and_nested_receipt_do_not_use_default_resources(self):
        project = load_project()
        project['id'] = 'example-mobile'
        project['runtime'].update(linuxUid=2001, roles={key: 'example-' + key for key in project['runtime']['roles']})
        project['signingBackup']['root'] = '/private/signing'
        config = compose_fixture(project)
        config['services']['example-android']['environment']['DELIVERY_STATE_ROOT'] = '/home/jenkins/agent/receipts/delivery-state'
        value = resources(config, project)
        self.assertEqual('example-mobile-home', value['home'])
        self.assertEqual('example-mobile-maintenance', value['maintenance'])
        self.assertEqual((2001, 'receipts/delivery-state'), (value['uid'], value['receiptRelative']))
        self.assertFalse(any('sxmqlive' in str(item) for item in value.values()))

    def test_missing_readonly_shared_or_unsafe_resources_are_refused(self):
        project = load_project()
        original = compose_fixture(project)
        role = project['runtime']['roles']['controller']
        mutations = (
            lambda c: c['services'].pop(project['runtime']['roles']['pr']),
            lambda c: c['services'][role]['volumes'][0].update(read_only=True),
            lambda c: c['services'][role]['volumes'][0].update(type='bind'),
            lambda c: c['volumes']['home'].update(name=c['volumes']['work']['name']),
            lambda c: c['services'][role].update(container_name='other;touch /unused'),
            lambda c: c['services'][role].update(container_name=c['services'][project['runtime']['roles']['android']]['container_name']),
            lambda c: c['services'][project['runtime']['roles']['android']]['environment'].update(DELIVERY_STATE_ROOT='/tmp/delivery-state'),
            lambda c: c['services'][project['runtime']['roles']['android']]['environment'].update(DELIVERY_STATE_ROOT='/home/jenkins/agent/../delivery-state'),
        )
        for mutation in mutations:
            config = copy.deepcopy(original)
            mutation(config)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                resources(config, project)


if __name__ == '__main__':
    unittest.main()
