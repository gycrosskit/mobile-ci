"""受信实例清单：离线验证与固定模板渲染，没有默认 apply 或可执行配置字段。"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import platform as host_platform
import pwd
import re
import stat
import sys
from urllib.parse import urlsplit

from project_config import PROJECT_ID, ROLE_PIPELINES, ADMIN_PROJECTS_DIR, PROJECTS_DIR, _read_static_json, load_project, matches, validate_project
from toolchains import component_for, load_toolchain_lock, lock_digest, preflight as toolchain_preflight

ROOT = Path(__file__).resolve().parent
JOB_LABELS = {
    'android_test': 'android-builder', 'android_release': 'android-builder',
    'ios_test': 'mac-build', 'ios_release': 'mac-build',
    'harmony_test': 'linux-build', 'harmony_release': 'linux-build',
    'app_pr': 'pr-builder', 'ci_pr': 'pr-builder', 'wiki': 'pr-builder',
    'signing_maintenance': 'signing-maintenance', 'signing_backup': 'signing-maintenance', 'alert': 'linux-build',
}
JOB_CAPABILITIES = {
    'android_test': 'android', 'android_release': 'android',
    'ios_test': 'ios', 'ios_release': 'ios', 'harmony_test': 'harmony', 'harmony_release': 'harmony',
    'app_pr': 'android', 'ci_pr': 'ci', 'wiki': 'ci', 'signing_maintenance': 'maintenance',
    'signing_backup': 'maintenance', 'alert': 'ci',
}


class DeploymentConfigError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise DeploymentConfigError(message)


def fields(value, expected, name):
    require(isinstance(value, dict) and set(value) == set(expected), name + ' 字段缺失或未知')


def path(value, name):
    require(isinstance(value, str) and matches(r'/[A-Za-z0-9_./-]+', value)
            and value == str(PurePosixPath(value)) and all(p not in {'.', '..', ''} for p in value.split('/')[1:])
            and value not in {'/', '/etc', '/usr', '/var', '/Users', '/home', '/tmp', '/private/tmp'}, name + ' 必须是固定非越界绝对路径')
    return value


def under(value, root):
    return value.startswith(root.rstrip('/') + '/')


def private_file(value):
    p = Path(value)
    require(p.is_absolute() and p.is_file() and p.resolve() == p, '实例文件必须是无 symlink 的绝对路径')
    st, parent = p.stat(), p.parent.stat()
    require(st.st_uid == os.getuid() and st.st_nlink == 1 and stat.S_IMODE(st.st_mode) == 0o600
            and parent.st_uid == os.getuid() and stat.S_IMODE(parent.st_mode) == 0o700, '实例目录/文件必须当前用户所有且为0700/0600')
    for parent in p.parents:
        info = parent.stat()
        require(info.st_uid in {0, os.getuid()} and not info.st_mode & 0o022, '实例目录祖先必须受信所有且不可由其他用户写入')
    return p


def platform(value):
    fields(value, {'os', 'arch'}, 'platform')
    require(value['os'] in {'linux', 'darwin'} and value['arch'] in {'amd64', 'arm64'}, '不支持的平台')


def identity(node, controller=False):
    require(matches(r'[a-z][a-z0-9-]{0,63}', node['id']) and matches(r'[A-Za-z0-9_.-]{1,64}', node['name']) and node['name'] not in {'.', '..'}, '无效节点身份')
    platform(node['host'])
    platform(node['target'])
    fields(node['service'], {'user', 'uid', 'gid', 'home'}, 'service')
    s = node['service']
    require(matches(r'_?[a-z][a-z0-9_-]{0,31}', s['user']) and s['user'] != 'root'
            and all(type(s[k]) is int and 0 < s[k] < 65536 for k in ('uid', 'gid')), '必须使用独立非root服务账号')
    path(s['home'], 'HOME')
    fields(node['resources'], {'cpu', 'memoryGiB', 'executors'}, 'resources')
    r = node['resources']
    require(type(r['cpu']) is int and 1 <= r['cpu'] <= 128 and type(r['memoryGiB']) is int and 2 <= r['memoryGiB'] <= 1024
            and type(r['executors']) is int and r['executors'] == (0 if controller else 1), '资源预算无效；Controller零Executor，Agent单Executor')
    if node['runtime'] == 'compose':
        require(node['target']['os'] == 'linux', 'Compose target 必须是Linux VM/container平台')
        require(s['home'] == ('/var/jenkins_home' if controller else '/home/jenkins'), '固定镜像HOME路径不能改写')
        require(s['user'] == 'jenkins' and s['uid'] == s['gid'] == 1000, '固定Linux镜像使用jenkins:1000:1000')
        require(matches(r'sha256:[0-9a-f]{64}', node['imageId']), 'Compose需要管理员验收的完整镜像ID，不能使用浮动tag或base image')
    else:
        require(not controller and node['runtime'] == 'native' and node['host'] == node['target']
                and node['target']['os'] == 'darwin' and s['uid'] >= 501, 'native仅支持独立macOS服务账号')
        require(s['home'].startswith(('/Users/Shared/', '/private/var/lib/mobile-ci/')), 'Mac服务HOME必须在独立服务目录')


def _validate_deployment(value):
    fields(value, {'version', 'id', 'primaryProjectId', 'controller', 'agents', 'profiles'}, 'deployment')
    require(type(value['version']) is int and value['version'] == 1 and isinstance(value['id'], str)
            and PROJECT_ID.fullmatch(value['id']), '无效实例版本或ID')
    c = value['controller']
    fields(c, {'id', 'name', 'host', 'target', 'runtime', 'imageId', 'service', 'resources', 'stateRoot', 'url', 'httpPort'}, 'controller')
    identity(c, True)
    require(c['runtime'] == 'compose', 'Controller当前使用固定Linux容器，可运行在Mac Docker VM')
    path(c['stateRoot'], 'Controller stateRoot')
    require(type(c['httpPort']) is int and 1024 <= c['httpPort'] <= 65535, '无效Controller端口')
    require(isinstance(c['url'], str), 'Controller URL必须字符串')
    url = urlsplit(c['url'])
    require(url.scheme in {'http', 'https'} and bool(url.hostname) and not url.username and not url.password
            and not url.query and not url.fragment and url.path in {'', '/'} and (url.scheme == 'https' or url.hostname in {'127.0.0.1', 'localhost'}) and matches(r'https?://[A-Za-z0-9.-]+(?::[0-9]+)?/?', c['url']), 'Controller URL不能包含凭据或任意路径')
    agents = value['agents']
    require(isinstance(agents, list) and bool(agents), '实例必须显式声明Agent')
    ids, names, native_homes, secrets = {c['id']}, {c['name']}, [], []
    native_users, native_uids = set(), set()
    for a in agents:
        keys = {'id', 'name', 'host', 'target', 'runtime', 'service', 'resources', 'labels', 'capabilities', 'paths', 'sdks', 'secretRef', 'expectedVersions'}
        if isinstance(a, dict) and a.get('runtime') == 'compose':
            keys.add('imageId')
        else:
            keys.add('remotingSha256')
        fields(a, keys, 'agent')
        identity(a)
        versions = a['expectedVersions']
        require(isinstance(versions, dict) and set(versions) <= {'python', 'ruby', 'cocoapods'}
                and 'python' in versions and all(matches(r'[0-9]+[.][0-9]+[.][0-9]+', v) for v in versions.values()), '未冻结依赖须逐Agent声明expectedVersions')
        if a['runtime'] == 'native':
            require(a['sdks'].get('java', '').endswith('/bin/java'), 'native Java必须显式JDK/bin/java路径')
            require(matches(r'[0-9a-f]{64}', a['remotingSha256']), 'native Remoting必须管理员预检SHA256')
            require('ios' not in a['capabilities'] or {'ruby', 'cocoapods'} <= set(versions), 'iOS必须声明Ruby/CocoaPods精确版本')
        require(a['id'] not in ids and a['name'] not in names, '节点ID/name重复')
        ids.add(a['id']); names.add(a['name'])
        require(isinstance(a['labels'], list) and bool(a['labels']) and all(matches(r'[A-Za-z0-9_-]+', x) for x in a['labels'])
                and len(a['labels']) == len(set(a['labels'])), 'labels必须唯一固定标识')
        caps = a['capabilities']
        require(isinstance(caps, list) and bool(caps) and all(isinstance(x, str) for x in caps)
                and len(caps) == len(set(caps)) and set(caps) <= {'android', 'ios', 'harmony', 'ci', 'maintenance'}, '未知/重复capability')
        require('ios' not in caps or a['target']['os'] == 'darwin', 'iOS需要native Mac')
        require('harmony' not in caps or a['target'] == {'os': 'linux', 'arch': 'amd64'}, 'Mac/arm64鸿蒙尚无已验收官方工具链；不能声明可运行')
        require('android' not in caps or a['target']['os'] == 'darwin' or a['target']['arch'] == 'amd64', 'Linux Android目前需要amd64官方SDK')
        require('maintenance' not in caps or a['runtime'] == 'compose' and 'harmony' in caps,
                '现有维护入口需要Linux Harmony工具链，不能声明缺少SDK的maintenance-only节点')
        path_fields = {'work', 'cache', 'runtime', 'secret'}
        if 'harmony' in caps and 'maintenance' not in caps:
            path_fields.add('harmonyCache')
        if 'maintenance' in caps:
            path_fields.add('signing')
        fields(a['paths'], path_fields, 'paths')
        for key, p in a['paths'].items():
            path(p, key)
            require(under(p, a['service']['home']), 'Agent路径必须位于自己的HOME')
        ps = list(a['paths'].values())
        require(len(set(ps)) == len(ps) and not any(x != y and under(x, y) for x in ps for y in ps), 'Agent工作/缓存/运行/secret路径不能重叠')
        require(matches(r'[a-z][a-z0-9-]{0,63}', a['secretRef']) and a['secretRef'] not in secrets, '连接secretRef必须逐节点独占且不含secret内容')
        secrets.append(a['secretRef'])
        require(isinstance(a['sdks'], dict) and set(a['sdks']) <= {'java', 'remoting', 'android', 'harmony', 'xcode'}, 'SDK存在未知字段')
        required = {'java', 'remoting'} if a['runtime'] == 'native' else set()
        required |= {'android'} if 'android' in caps else set()
        required |= {'harmony'} if 'harmony' in caps else set()
        required |= {'xcode'} if 'ios' in caps else set()
        require(required <= set(a['sdks']), '声明能力缺少SDK位置')
        for key, p in a['sdks'].items():
            path(p, key)
        if a['runtime'] == 'native':
            require(a['service']['user'] not in native_users and a['service']['uid'] not in native_uids, 'native Agent服务用户名/UID必须独立')
            native_users.add(a['service']['user']); native_uids.add(a['service']['uid'])
            require(all(a['service']['home'] != h and not under(a['service']['home'], h) and not under(h, a['service']['home']) for h in native_homes), 'Mac Agent HOME不能共享或重叠')
            native_homes.append(a['service']['home'])
            require(all(under(p, a['service']['home']) for k, p in a['sdks'].items() if k not in {'java', 'xcode'}), 'Mac remoting/Android必须隔离在该Agent HOME')
    profiles = value['profiles']
    require(isinstance(profiles, dict) and bool(profiles), '必须显式绑定受信profile与任务')
    require(isinstance(value['primaryProjectId'], str) and value['primaryProjectId'] in profiles, 'primaryProjectId必须显式引用已绑定受信项目')
    by_id = {a['id']: a for a in agents}
    node_roles = {a['id']: set() for a in agents}
    for pid, bindings in profiles.items():
        require(isinstance(pid, str) and PROJECT_ID.fullmatch(pid) and isinstance(bindings, dict) and bool(bindings), '无效profile绑定')
        for role, aid in bindings.items():
            require(role in ROLE_PIPELINES and isinstance(aid, str) and aid in by_id, '未知Job角色或Agent引用')
            a = by_id[aid]
            require(JOB_CAPABILITIES[role] in a['capabilities'] and JOB_LABELS[role] in a['labels'], '任务能力/现有Pipeline label不匹配')
            require(not (role.startswith('android_') and a['target']['os'] != 'linux'), 'Android发布Pipeline的Linux预算尚未迁移；native Mac可配置但不能绑定此任务')
            require(not (role in {'app_pr', 'ci_pr'} and a['target']['os'] != 'linux'),
                    'PR主Pipeline仍使用Linux互斥/缓存或固定Groovy路径；native主节点尚未验收')
            require(not (role == 'signing_maintenance' and a['name'] != 'signing-maintenance'), '维护Pipeline仍要求signing-maintenance固定node name')
            if role in {'app_pr', 'ci_pr', 'wiki'}:
                require('ios-pr-builder' not in a['labels'], 'PR主节点不能是iOS PR子节点')
            candidates = [n for n in agents if JOB_LABELS[role] in n['labels'] and not (role in {'app_pr', 'ci_pr'} and 'ios-pr-builder' in n['labels'])]
            require(bool(candidates) and all(JOB_CAPABILITIES[role] in n['capabilities'] for n in candidates), 'Pipeline label pool存在能力不匹配节点')
            require(not role.startswith('android_') or all(n['target']['os'] == 'linux' for n in candidates), 'Android标签pool不能包含尚未迁移预算的Mac节点')
            node_roles[aid].add(role)
    for a in agents:
        require(not ('pr-builder' in a['labels'] and set(a['labels']) & {'android-builder', 'mac-build', 'linux-build', 'signing-maintenance'}), 'PR标签不能与生产构建/维护标签共用')
    for aid, roles in node_roles.items():
        pr = bool(roles & {'app_pr', 'ci_pr', 'wiki'})
        privileged = bool(roles & {'android_test', 'android_release', 'ios_test', 'ios_release', 'harmony_test', 'harmony_release', 'signing_maintenance', 'signing_backup'})
        require(not (pr and privileged), 'PR节点不能与签名/发布/维护共用')
    lock = load_toolchain_lock()
    component_for('controllerImage', c['target']['os'], c['target']['arch'], lock)
    for a in agents:
        component_for('jdk', a['target']['os'], a['target']['arch'], lock)
        if 'harmony' in a['capabilities']:
            component_for('harmonyCLT', a['target']['os'], a['target']['arch'], lock)
        if 'ios' in a['capabilities']:
            component_for('xcode', a['target']['os'], a['target']['arch'], lock)
        if a['runtime'] == 'compose':
            component_for('ohosAgentImage' if 'harmony' in a['capabilities'] else 'androidAgentImage', a['target']['os'], a['target']['arch'], lock)
    return copy.deepcopy(value)


def validate_deployment(value):
    try:
        return _validate_deployment(value)
    except (TypeError, KeyError, AttributeError) as error:
        raise DeploymentConfigError('实例字段类型无效') from error


def load_deployment(filename, private=False):
    p = private_file(filename) if private else Path(filename)
    try:
        value = _read_static_json(p, object_pairs_hook=unique_keys)
    except (OSError, json.JSONDecodeError) as e:
        raise DeploymentConfigError('无法读取实例JSON') from e
    return validate_deployment(value)


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'JSON重复字段')
        result[key] = value
    return result


def job_agent_bindings(manifest, project):
    if isinstance(project, str):
        project = load_project(project)
    require(project['id'] in manifest['profiles'], '实例未绑定该受信项目')
    bindings = manifest['profiles'][project['id']]
    require(set(bindings) == set(project['jobs']), '实例绑定必须覆盖且仅覆盖profile实际Job角色')
    agents = {a['id']: a for a in manifest['agents']}
    return {role: {'id': aid, 'name': agents[aid]['name'], 'label': JOB_LABELS[role], 'eligibleAgentIds': [a['id'] for a in manifest['agents'] if JOB_LABELS[role] in a['labels'] and not (role in {'app_pr', 'ci_pr'} and 'ios-pr-builder' in a['labels'])]} for role, aid in bindings.items()}


def node_environment(a, url):
    env = {'HOME': a['service']['home'], 'JENKINS_URL': url, 'JENKINS_AGENT_NAME': a['name'],
           'JENKINS_AGENT_WORKDIR': a['paths']['work'], 'JENKINS_AGENT_WORK_DIR': a['paths']['work'],
           'JENKINS_AGENT_SECRET_FILE': a['paths']['secret'] + '/agent.secret', 'GRADLE_USER_HOME': a['paths']['cache'],
           'DELIVERY_STATE_ROOT': a['paths']['work'] + '/delivery-state'}
    for key, variable in {'android': 'ANDROID_SDK_ROOT', 'harmony': 'HARMONY_CLT_HOME', 'xcode': 'DEVELOPER_DIR'}.items():
        if key in a['sdks']:
            env[variable] = a['sdks'][key]
    if a['runtime'] == 'native':
        env['JAVA_HOME'] = a['sdks']['java'][:-len('/bin/java')]
        env['PATH'] = a['sdks']['java'][:-len('/java')] + ':/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin' + (':' + a['sdks']['android'] + '/cmdline-tools/latest/bin:' + a['sdks']['android'] + '/platform-tools' if 'android' in a['sdks'] else '')
    if 'harmonyCache' in a['paths']:
        env['HARMONY_GRADLE_USER_HOME'] = a['paths']['harmonyCache']
    if 'signing' in a['paths']:
        env['SIGNING_MAINTENANCE_ROOT'] = a['paths']['signing']
    if a['runtime'] == 'compose' and 'maintenance' not in a['capabilities']:
        env['CI_LINUX_MEMORY_LOCK'] = '/home/jenkins/linux-build-memory/build.lock'
    return env


def serialized(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def derived_nodes(m):
    return {'version': 1, 'deploymentId': m['id'],
            'deploymentSha256': hashlib.sha256(serialized(m).encode('utf-8')).hexdigest(),
            'nodes': [{key: a[key] for key in ('id', 'name', 'labels', 'capabilities', 'target')} |
                      {'remoteFS': a['paths']['work'], 'executors': a['resources']['executors']} for a in m['agents']]}


def render_compose(m):
    c = m['controller']; services = {}; volumes = {}
    profile_sources = {pid: PROJECTS_DIR / (pid + '.json') if (PROJECTS_DIR / (pid + '.json')).is_file() else ADMIN_PROJECTS_DIR / (pid + '.json') for pid in m['profiles']}
    profile_hashes = {pid: hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else 'unverified' for pid, source in profile_sources.items()}
    def volume(name, target, readonly=False):
        volumes[name] = {'name': m['id'] + '_' + name}
        return {'type': 'volume', 'source': name, 'target': target, 'read_only': readonly, 'volume': {'nocopy': True}}
    controller_mounts = [volume('controller-home', c['service']['home']),
                {'type': 'bind', 'source': str(ROOT / 'jenkins/controller/init.groovy.d/06-configure-deployment-agents.groovy'), 'target': c['service']['home'] + '/init.groovy.d/06-configure-deployment-agents.groovy', 'read_only': True}]
    controller_mounts.append({'type': 'bind', 'source': str(ROOT / 'jenkins/controller/init.groovy.d/02-configure-gitee.groovy'), 'target': c['service']['home'] + '/init.groovy.d/02-configure-gitee.groovy', 'read_only': True})
    for a in m['agents']:
        controller_mounts.append(volume(a['secretRef'], '/run/mobile-ci/' + a['id']))
    services[c['id']] = {'image': c['imageId'], 'platform': 'linux/' + c['target']['arch'], 'user': '1000:1000', 'init': True,
        'restart': 'unless-stopped', 'cpus': c['resources']['cpu'], 'mem_limit': str(c['resources']['memoryGiB']) + 'g',
        'ports': ['127.0.0.1:' + str(c['httpPort']) + ':8080'],
        'environment': {'JENKINS_HOME': c['service']['home'], 'JENKINS_CI_PROJECT': m['primaryProjectId'], 'MOBILE_CI_DEPLOYMENT_FILE': c['service']['home'] + '/ci-deployment/nodes.json', 'MOBILE_CI_NODES_SHA256': hashlib.sha256(serialized(derived_nodes(m)).encode('utf-8')).hexdigest(), 'MOBILE_CI_DEPLOYMENT_SHA256': hashlib.sha256(serialized(m).encode('utf-8')).hexdigest(), 'JAVA_OPTS': '-Djava.awt.headless=true'},
        'volumes': controller_mounts,
        'healthcheck': {'test': ['CMD', 'curl', '--fail', '--silent', 'http://127.0.0.1:8080/login'], 'interval': '30s', 'timeout': '10s', 'retries': 10, 'start_period': '90s'}}
    # 固定现有入口要求私有锁卷；不同Agent仍共享同一资源预算锁。
    init_mounts = [volume('memory-lock', '/memory'), volume('controller-home', '/init/controller-home')]
    init_dirs = ['controller-home']
    init_mounts += [{'type': 'bind', 'source': c['stateRoot'] + '/' + name, 'target': '/source/' + name, 'read_only': True} for name in ('nodes.json', 'deployment.json')]
    init_mounts.append({'type': 'bind', 'source': str(ROOT / 'jenkins/remote/init-deployment-state.sh'), 'target': '/usr/local/lib/mobile-ci/init-state.sh', 'read_only': True})
    for pid in m['profiles']:
        init_mounts.append({'type': 'bind', 'source': str(profile_sources[pid]), 'target': '/source/profiles/' + pid + '.json', 'read_only': True})
    for a in m['agents']:
        init_mounts.append(volume(a['secretRef'], '/init/' + a['secretRef']))
        init_dirs.append(a['secretRef'])
        if a['runtime'] != 'compose':
            continue
        mounts = [volume(a['id'] + '-profiles', '/etc/mobile-ci', True)]
        init_mounts.append(volume(a['id'] + '-profiles', '/profiles/' + a['id']))
        for key in (key for key in a['paths'] if key != 'secret'):
            name = a['id'] + '-' + key
            mounts.append(volume(name, a['paths'][key]))
            init_mounts.append(volume(name, '/init/' + name))
            init_dirs.append(name)
        mounts.append(volume(a['secretRef'], a['paths']['secret'], True))
        if 'maintenance' not in a['capabilities']:
            mounts.append(volume('memory-lock', '/home/jenkins/linux-build-memory'))
        for key in ('android', 'harmony'):
            if key in a['sdks']:
                mounts.append({'type': 'bind', 'source': c['stateRoot'] + '/sdks/' + key, 'target': a['sdks'][key], 'read_only': True})
        services[a['id']] = {'image': a['imageId'], 'platform': 'linux/' + a['target']['arch'], 'user': '1000:1000', 'init': True,
            'restart': 'unless-stopped', 'cpus': a['resources']['cpu'], 'mem_limit': str(a['resources']['memoryGiB']) + 'g',
            'environment': node_environment(a, 'http://' + c['id'] + ':8080'), 'volumes': mounts,
            'depends_on': {'state-init': {'condition': 'service_completed_successfully'}, c['id']: {'condition': 'service_healthy'}}}
    services['state-init'] = {'image': c['imageId'], 'user': '0:0', 'restart': 'no', 'entrypoint': ['/bin/bash', '/usr/local/lib/mobile-ci/init-state.sh'],
        'command': [hashlib.sha256(serialized(derived_nodes(m)).encode('utf-8')).hexdigest(), hashlib.sha256(serialized(m).encode('utf-8')).hexdigest(), ','.join(sorted(m['profiles'])), ','.join(a['id'] for a in m['agents'] if a['runtime'] == 'compose'), ','.join(pid + ':' + profile_hashes[pid] for pid in sorted(m['profiles'])), *init_dirs], 'volumes': init_mounts}
    services[c['id']]['depends_on'] = {'state-init': {'condition': 'service_completed_successfully'}}
    return {'name': m['id'], 'services': services, 'volumes': volumes}


def render_native(a, url):
    env = node_environment(a, url)
    label = 'com.mobile-ci.agent.' + a['id']
    plist = {'Label': label, 'UserName': a['service']['user'],
             'ProgramArguments': [a['sdks']['java'], '-jar', a['sdks']['remoting'], '-url', url,
                                  '-secret', '@' + env['JENKINS_AGENT_SECRET_FILE'], '-name', a['name'], '-webSocket', '-workDir', a['paths']['work']],
             'EnvironmentVariables': env, 'WorkingDirectory': a['paths']['work'], 'RunAtLoad': True, 'KeepAlive': True,
             'StandardOutPath': a['paths']['runtime'] + '/agent.log', 'StandardErrorPath': a['paths']['runtime'] + '/agent.err.log'}
    return {'environment': env, 'launchdLabel': label, 'plist': plistlib.dumps(plist).decode('utf-8')}


def plan(m, example_profile=None):
    verify_trusted = example_profile is None
    if verify_trusted:
        verify_profiles(m)
    return {'deploymentId': m['id'], 'productionTouched': False, 'applySupported': False, 'toolchainLockSha256': lock_digest(load_toolchain_lock()),
            'services': [m['controller']['id']] + [a['id'] for a in m['agents'] if a['runtime'] == 'compose'],
            'nativeAgents': [a['id'] for a in m['agents'] if a['runtime'] == 'native'],
            'trustedProfilesVerified': verify_trusted, 'testOnly': not verify_trusted, 'offline': True,
            'profiles': {pid: job_agent_bindings(m, load_project(pid) if verify_trusted else example_profile) for pid in m['profiles']},
            'requiredChecks': ['独立管理员审核完整镜像ID、toolchains.lock与SDK实测版本', '部署前私有0700目录/0600实例与secret；安装服务账号和SDK；新Job默认禁用，管理员核对精确审批后才enable',
                               'Controller/node连接、Executor、label、预算与只读SDK运行验收', 'Mac鸿蒙当前拒绝，官方Mac工具支持须另行probe和Pipeline迁移']}


def verify_profiles(m):
    for pid in m['profiles']:
        p = load_project(pid)
        require(not p.get('testOnly'), 'testOnly profile不能用于运行部署')
        job_agent_bindings(m, p)


def write_render(m, directory):
    verify_profiles(m)
    out = Path(directory)
    require(str(out) == m['controller']['stateRoot'] and out.is_dir() and out.resolve() == out,
            '输出目录必须为清单stateRoot且存在、无symlink')
    st = out.stat()
    require(st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o700, '输出目录必须当前用户所有、0700')
    for parent in out.parents:
        info = parent.stat()
        require(info.st_uid in {0, os.getuid()} and not info.st_mode & 0o022, '输出目录祖先不可由其他用户写入')
    artifacts = {'deployment.json': serialized(m).rstrip('\n'), 'nodes.json': serialized(derived_nodes(m)).rstrip('\n'),
                 'compose.json': serialized(render_compose(m)).rstrip('\n')}
    for a in m['agents']:
        if a['runtime'] == 'native':
            native = render_native(a, m['controller']['url'])
            artifacts['native.' + a['id'] + '.plist'] = native['plist']
            artifacts['native.' + a['id'] + '.env.json'] = json.dumps(native['environment'], indent=2)
    require(all(not (out / name).exists() and not (out / name).is_symlink() for name in artifacts), '输出目标已存在；禁止覆盖实例/运行材料')
    created = []
    try:
        for name, contents in artifacts.items():
            filename = out / name
            fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created.append(filename)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(contents + '\n')
    except OSError:
        for filename in created:
            filename.unlink()
        raise DeploymentConfigError('安全渲染文件失败')
    return {'outputDirectory': str(out), 'artifacts': sorted(artifacts), 'applySupported': False}


def preflight_agent(m, agent_id):
    agents = [a for a in m['agents'] if a['id'] == agent_id]
    require(len(agents) == 1, '未知Agent ID')
    a = agents[0]
    require(a['runtime'] == 'native', 'Compose SDK预检必须在对应容器内执行；不能用宿主probe代替')
    host = {'os': host_platform.system().lower(), 'arch': {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(host_platform.machine())}
    require(host == a['target'], '当前实际宿主OS/arch与所选native Agent不匹配')
    service = pwd.getpwuid(os.getuid())
    require(os.getuid() == a['service']['uid'] and os.getgid() == a['service']['gid']
            and service.pw_name == a['service']['user'] and service.pw_dir == a['service']['home'] and service.pw_gid == a['service']['gid'], '须以所选Agent实际独立服务账号执行probe')
    for parent in Path(a['service']['home']).parents:
        info = parent.stat()
        require(not parent.is_symlink() and info.st_uid in {0, os.getuid()} and not info.st_mode & 0o022, 'native HOME祖先必须受信所有且不可由其他用户写入')
    for p in list(a['paths'].values()) + [a['service']['home']]:
        directory = Path(p)
        require(directory.is_dir() and directory.resolve() == directory and directory.stat().st_uid == os.getuid()
                and stat.S_IMODE(directory.stat().st_mode) == 0o700, '服务HOME/运行路径必须已安装且为当前服务账号所有的0700目录')
    caps = a['capabilities']
    roles = (['android'] if 'android' in caps else []) + (['ios'] if 'ios' in caps else [])
    if not roles and 'ci' in caps:
        roles = ['pr']
    require(bool(roles), '该native Agent能力尚无对应受信工具链probe')
    original_env = dict(os.environ)
    try:
        os.environ.update(node_environment(a, m['controller']['url']))
        reports = [toolchain_preflight(role, sdk_root=a['sdks'].get('android'), java_bin=a['sdks']['java'],
                                       remoting_jar=a['sdks']['remoting'], remoting_sha256=a['remotingSha256'],
                                       expected_versions=a['expectedVersions']) for role in roles]
    finally:
        os.environ.clear(); os.environ.update(original_env)
    return {'agentId': a['id'], 'scope': 'selected-native-agent-runtime-preflight', 'profileReferencesVerified': False,
            'productionTouched': False, 'reports': reports}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['validate', 'render', 'plan', 'services', 'preflight'])
    parser.add_argument('manifest')
    parser.add_argument('--private', action='store_true')
    parser.add_argument('--output-dir')
    parser.add_argument('--example', action='store_true')
    parser.add_argument('--agent')
    args = parser.parse_args(argv)
    try:
        require(not args.example or args.action == 'plan' and not args.private and not args.output_dir, '--example仅允许公开离线plan')
        if args.example:
            require(Path(args.manifest).absolute() in {ROOT / 'examples/deployment/linux.json', ROOT / 'examples/deployment/linux-with-remote-mac.json'}, '--example仅允许固定公开示例')
        m = load_deployment(args.manifest, args.private)
        require(not args.output_dir or args.action == 'render', '--output-dir仅用于显式render')
        require(not args.agent or args.action == 'preflight', '--agent仅用于所选节点preflight')
        if args.action == 'preflight':
            require(args.private and args.agent is not None, 'preflight必须显式--private和--agent')
            result = preflight_agent(m, args.agent)
        elif args.action == 'validate':
            result = {'valid': True, 'id': m['id']}
        elif args.action == 'services':
            print(' '.join([m['controller']['id']] + [a['id'] for a in m['agents'] if a['runtime'] == 'compose']))
            return 0
        elif args.action == 'plan':
            require(args.private or args.example, '真实plan必须显式--private，公开示例使用--example')
            example = validate_project(_read_static_json(ROOT / 'examples/second-project/project.json')) if args.example else None
            require(example is None or example.get('testOnly') is True and set(m['profiles']) == {example['id']}, '示例必须使用固定testOnly profile')
            result = plan(m, example_profile=example)
        elif args.output_dir:
            require(args.private, '写部署产物必须显式--private受信实例')
            result = write_render(m, args.output_dir)
        else:
            result = {'compose': render_compose(m), 'native': {a['id']: render_native(a, m['controller']['url']) for a in m['agents'] if a['runtime'] == 'native'}}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DeploymentConfigError, ValueError, KeyError, TypeError, OSError) as e:
        print('Deployment rejected: ' + str(e), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
