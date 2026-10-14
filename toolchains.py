#!/usr/bin/env python3
"""管理员工具链锁、构建参数及只读主机预检；候选 Job 不能自由指定工具版本。"""
import argparse
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
LOCK_PATH = ROOT / 'toolchains.lock.json'
PLATFORMS = {'linux/amd64', 'linux/arm64', 'darwin/amd64', 'darwin/arm64'}
SCOPES = {'exact-version', 'major-version', 'minimum-version', 'managed-unpinned',
          'reviewed-tag', 'digest', 'archive-digest', 'file-digest', 'clt-bundled', 'controller-coupled'}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate toolchain key: ' + key)
        result[key] = value
    return result


def validate_toolchain_lock(lock):
    if not isinstance(lock, dict) or set(lock) != {'schemaVersion', 'components'} or type(lock['schemaVersion']) is not int or lock['schemaVersion'] != 1:
        raise ValueError('Unsupported toolchain lock schema')
    components = lock['components']
    if not isinstance(components, dict) or not components:
        raise ValueError('Toolchain components must be a nonempty object')
    required = {'controllerImage', 'androidAgentImage', 'ohosAgentImage', 'jenkins', 'jenkinsPlugins',
                'jdk', 'androidCommandLineTools', 'androidSDK', 'androidBuildTools', 'androidNDK',
                'xcode', 'fastlane', 'harmonyCLT', 'harmonyNode', 'ohpm', 'hvigor', 'harmonySDK', 'groovy', 'dws', 'python'}
    if not required <= set(components):
        raise ValueError('Missing required toolchain components')
    for name, component in components.items():
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9]*', name) or not isinstance(component, dict):
            raise ValueError('Invalid toolchain component')
        if not {'version', 'kind', 'platforms', 'pinScope'} <= set(component) or set(component) - {
                'version', 'kind', 'platforms', 'pinScope', 'imageRef', 'sha256', 'licenseSha256', 'url', 'note'}:
            raise ValueError('Invalid component fields: ' + name)
        if not isinstance(component['kind'], str) or not isinstance(component['pinScope'], str) or component['kind'] not in {'tool', 'image', 'image-base'} or component['pinScope'] not in SCOPES:
            raise ValueError('Invalid component kind/pin scope: ' + name)
        hosts = component['platforms']
        if not isinstance(hosts, list) or not hosts or any(not isinstance(host, str) or host not in PLATFORMS for host in hosts) or len(set(hosts)) != len(hosts):
            raise ValueError('Invalid OS/architecture compatibility: ' + name)
        version = component['version']
        if version is not None and (not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', version)):
            raise ValueError('Invalid locked version: ' + name)
        if version is None and component['pinScope'] not in {'managed-unpinned', 'controller-coupled'}:
            raise ValueError('Fixed component needs a version: ' + name)
        for field in ('sha256', 'licenseSha256'):
            if field in component and (not isinstance(component[field], str) or not re.fullmatch(r'[0-9a-f]{64}', component[field])):
                raise ValueError('Invalid SHA256: ' + name)
        if component['pinScope'] in {'digest', 'archive-digest', 'file-digest'} and 'sha256' not in component:
            raise ValueError('Digest pin requires SHA256: ' + name)
        if component['kind'] in {'image', 'image-base'}:
            image = component.get('imageRef', '')
            if not isinstance(image, str) or not re.fullmatch(r'[A-Za-z0-9./_-]+:[A-Za-z0-9._-]+(?:@sha256:[0-9a-f]{64})?', image) or ':latest' in image:
                raise ValueError('Image requires a fixed tag or digest: ' + name)
            if component['pinScope'] == 'digest' and not image.endswith('@sha256:' + component['sha256']):
                raise ValueError('Image digest disagrees with SHA256: ' + name)
        if 'url' in component:
            if not isinstance(component['url'], str):
                raise ValueError('Artifact URL must be a string: ' + name)
            url = urlsplit(component['url'])
            if url.scheme != 'https' or url.hostname not in {'dl.google.com', 'repo.maven.apache.org', 'github.com', 'developer.huawei.com'} or url.username or url.password or url.port not in {None, 443} or url.query or url.fragment:
                raise ValueError('Artifact URL requires an official HTTPS host without credentials/query/fragment: ' + name)
    return lock


def load_toolchain_lock(path=LOCK_PATH):
    path = Path(path).absolute()
    if path.parent.resolve() != path.parent:
        raise ValueError('Toolchain lock parents must not contain symlinks')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
            raise ValueError('Toolchain lock must be a single regular file below 1 MiB')
        data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError('Toolchain lock exceeds 1 MiB')
    return validate_toolchain_lock(json.loads(data, object_pairs_hook=_unique_object))


def lock_digest(lock):
    return hashlib.sha256(json.dumps(lock, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def component_for(component_id, os_name, arch, lock=None):
    lock = load_toolchain_lock() if lock is None else validate_toolchain_lock(lock)
    component = lock['components'].get(component_id)
    if component is None:
        raise ValueError('Unknown toolchain component: ' + component_id)
    if os_name + '/' + arch not in component['platforms']:
        raise ValueError('Unsupported OS/architecture for ' + component_id + ': ' + os_name + '/' + arch)
    return component


def locked_version(component_id):
    component = load_toolchain_lock()['components'].get(component_id)
    if not component or component['version'] is None:
        raise ValueError('Component has no exact/required version: ' + component_id)
    return component['version']


def docker_build_args(role, lock=None):
    lock = load_toolchain_lock() if lock is None else validate_toolchain_lock(lock)
    components = lock['components']
    if role == 'controller':
        return {'JENKINS_CONTROLLER_IMAGE': components['controllerImage']['imageRef']}
    if role == 'ohos':
        return {'OHOS_JDK_IMAGE': components['ohosAgentImage']['imageRef']}
    if role != 'android':
        raise ValueError('Unknown Docker build role: ' + role)
    bindings = {'ANDROID_COMMAND_LINE_TOOLS': 'androidCommandLineTools',
                'CI_GROOVY': 'groovy', 'DWS': 'dws'}
    values = {'JENKINS_AGENT_IMAGE': components['androidAgentImage']['imageRef'],
              'ANDROID_PLATFORM_VERSION': components['androidSDK']['version'],
              'ANDROID_BUILD_TOOLS_VERSION': components['androidBuildTools']['version'],
              'ANDROID_NDK_VERSION': components['androidNDK']['version']}
    for prefix, name in bindings.items():
        values[prefix + '_VERSION'] = components[name]['version']
        values[('DWS_LINUX_AMD64' if prefix == 'DWS' else prefix) + '_SHA256'] = components[name]['sha256']
    return values


def _run(command, expected=None, environment=None):
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60, env=environment)
    output = result.stdout.strip()
    if result.returncode or not output or expected and not re.search(expected, output, re.M):
        raise ValueError('Runtime probe failed: ' + str(command[0]))
    return output[:2048]


def _revision(path):
    text = Path(path).read_text()
    match = re.search(r'^Pkg.Revision\s*=\s*(\S+)\s*$', text, re.M)
    if not match:
        raise ValueError('Missing SDK revision: ' + str(path))
    return match.group(1)


def preflight(role, sdk_root=None, clt_root=None, java_bin='java', jenkins_war=None,
              remoting_jar=None, remoting_sha256=None, expected_versions=None):
    """探测实际执行主机；返回版本事实，不宣称构建、签名或发行验收。"""
    expected_versions = {} if expected_versions is None else validate_expected_versions(expected_versions)
    lock = load_toolchain_lock()
    os_name = platform.system().lower()
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine(), platform.machine())
    component_for('jdk', os_name, arch, lock)
    if sys.version_info < (3, 9):
        raise ValueError('Preflight interpreter requires Python 3.9 or newer')
    python_output = _run(['python3', '--version'], r'^Python [0-9]+\.[0-9]+\.[0-9]+$')
    python_match = re.fullmatch(r'Python ([0-9]+\.[0-9]+\.[0-9]+)', python_output)
    if python_match is None:
        raise ValueError('Runtime Python version is invalid')
    observations = {'jdk': _run([java_bin, '-version'], r'version "' + re.escape(lock['components']['jdk']['version']) + r'(?:\.|\")'),
                    'python': python_match.group(1), 'probeInterpreter': platform.python_version(),
                    'git': _run(['git', '--version'])}
    minimum = tuple(int(value) for value in lock['components']['python']['version'].split('.'))
    if tuple(int(value) for value in observations['python'].split('.'))[:len(minimum)] < minimum:
        raise ValueError('Python is below locked minimum')
    if role == 'controller':
        if jenkins_war is None:
            raise ValueError('Controller probe requires --jenkins-war')
        observations['jenkins'] = _run([java_bin, '-jar', str(jenkins_war), '--version'],
                                      '^' + re.escape(locked_version('jenkins')) + '$')
    elif role in {'ios', 'android', 'ohos', 'pr'}:
        if remoting_jar is None or not re.fullmatch(r'[0-9a-f]{64}', remoting_sha256 or ''):
            raise ValueError('Agent probe requires reviewed remoting jar and SHA256')
        jar = Path(remoting_jar)
        if jar.is_symlink() or not jar.is_file() or hashlib.sha256(jar.read_bytes()).hexdigest() != remoting_sha256:
            raise ValueError('Remoting jar SHA256 mismatch')
        observations['remoting'] = _run([java_bin, '-jar', str(jar), '-version'])
        observations['remotingSha256'] = remoting_sha256
    else:
        raise ValueError('Unknown preflight role: ' + role)
    if role == 'ios':
        component_for('xcode', os_name, arch, lock)
        observations['xcode'] = _run(['xcodebuild', '-version'], '^Xcode ' + re.escape(locked_version('xcode')) + '$')
        subprocess.run(['xcodebuild', '-checkFirstLaunchStatus'], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        observations['iphoneosSDK'] = _run(['xcrun', '--sdk', 'iphoneos', '--show-sdk-path'])
        if not Path(observations['iphoneosSDK']).is_dir():
            raise ValueError('Resolved iPhoneOS SDK directory is missing')
        ruby_bin = str(Path(_run(['brew', '--prefix', 'ruby'])) / 'bin/ruby')
        for name, command in [('brew', ['brew', '--version']), ('ruby', [ruby_bin, '--version']), ('cocoapods', ['pod', '--version'])]:
            observations[name] = _run(command)
        observations['fastlane'] = _run(['bash', str(ROOT / 'delivery/fastlane.sh'), '--version'],
                                        r'fastlane ' + re.escape(locked_version('fastlane')) + r'$',
                                        {**os.environ, 'FASTLANE_RUBY': ruby_bin})
    if role == 'android':
        component_for('androidSDK', os_name, arch, lock)
        if sdk_root is None:
            raise ValueError('Android probe requires --sdk-root')
        sdk = Path(sdk_root)
        for name, relative in [('androidSDK', 'platforms/android-' + locked_version('androidSDK')),
                               ('androidBuildTools', 'build-tools/' + locked_version('androidBuildTools')),
                               ('androidNDK', 'ndk/' + locked_version('androidNDK'))]:
            revision = _revision(sdk / relative / 'source.properties')
            if name != 'androidSDK' and revision != locked_version(name):
                raise ValueError('Android SDK version mismatch: ' + name)
            observations[name] = revision
        android_properties = (sdk / ('platforms/android-' + locked_version('androidSDK')) / 'source.properties').read_text()
        if not re.search(r'^AndroidVersion.ApiLevel\s*=\s*' + locked_version('androidSDK') + r'\s*$', android_properties, re.M):
            raise ValueError('Android API version mismatch')
        observations['androidPlatformTools'] = _revision(sdk / 'platform-tools/source.properties')
        observations['androidCommandLineTools'] = _revision(sdk / 'cmdline-tools/latest/source.properties')
        observations['sdkmanager'] = _run([str(sdk / 'cmdline-tools/latest/bin/sdkmanager'), '--version'])
        host = 'darwin-x86_64' if os_name == 'darwin' else 'linux-x86_64'
        observations['ndkClang'] = _run([str(sdk / ('ndk/' + locked_version('androidNDK')) / 'toolchains/llvm/prebuilt' / host / 'bin/clang'), '--version'])
    if role == 'ohos':
        component_for('harmonyCLT', os_name, arch, lock)
        if clt_root is None:
            raise ValueError('OHOS probe requires --clt-root')
        observations['harmonyCLT'] = _run([sys.executable, str(ROOT / 'jenkins/remote/ohos-agent/clt.py'), 'check', str(clt_root), '--runtime'])
    for name, expected in expected_versions.items():
        if name not in observations or not re.search(r'(?<![0-9.])' + re.escape(expected) + r'(?![0-9.])', observations[name]):
            raise ValueError('Administrator expected version mismatch: ' + name)
    return {'expectedVersions': expected_versions, 'role': role, 'platform': os_name + '/' + arch, 'lockSha256': lock_digest(lock),
            'observations': observations, 'scope': 'runtime-preflight-only',
            'unfrozenDependencies': [name for name, value in lock['components'].items()
                                     if value['pinScope'] in {'managed-unpinned', 'minimum-version', 'major-version', 'reviewed-tag', 'controller-coupled'}]}


def check_repository_pins(lock=None):
    """兼容文件仍供 Fastfile/独立 CLT 使用，晋级时必须与唯一版本清单同步。"""
    import ast
    lock = load_toolchain_lock() if lock is None else validate_toolchain_lock(lock)
    components = lock['components']
    for name, relative in [('fastlane', 'delivery/.fastlane-version'), ('xcode', 'jenkins/local/.xcode-version')]:
        if (ROOT / relative).read_text().strip() != components[name]['version']:
            raise ValueError('Compatibility version file differs from lock: ' + name)
    if hashlib.sha256((ROOT / 'jenkins/controller/plugins.txt').read_bytes()).hexdigest() != components['jenkinsPlugins']['sha256']:
        raise ValueError('Controller plugin list differs from reviewed lock')
    if 'brew "openjdk@' + components['jdk']['version'] + '"' not in (ROOT / 'jenkins/local/Brewfile').read_text():
        raise ValueError('Mac Brew JDK requirement differs from reviewed lock')
    clt_source = (ROOT / 'jenkins/remote/ohos-agent/clt.py').read_text()
    constants = {node.targets[0].id: ast.literal_eval(node.value) for node in ast.parse(clt_source).body
                 if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                 and node.targets[0].id in {'VERSION', 'SHA256', 'LICENSE_SHA256', 'NODE_VERSION', 'OHPM_VERSION', 'HVIGOR_VERSION', 'SDK_VERSION'}}
    for field, constant in [('version', 'VERSION'), ('sha256', 'SHA256'), ('licenseSha256', 'LICENSE_SHA256')]:
        if components['harmonyCLT'][field] != constants[constant]:
            raise ValueError('CLT installer differs from reviewed lock: ' + field)
    for name, constant in [('harmonyNode', 'NODE_VERSION'), ('ohpm', 'OHPM_VERSION'), ('hvigor', 'HVIGOR_VERSION'), ('harmonySDK', 'SDK_VERSION')]:
        if components[name]['version'] != constants[constant]:
            raise ValueError('Bundled CLT component differs from reviewed lock: ' + name)
    for role, relative in [('controller', 'jenkins/controller/Dockerfile'),
                           ('android', 'jenkins/remote/android-agent/Dockerfile'),
                           ('ohos', 'jenkins/remote/ohos-agent/Dockerfile')]:
        args = dict(re.findall(r'^ARG ([A-Z0-9_]+)=(.+)$', (ROOT / relative).read_text(), re.M))
        if any(args.get(name) != value for name, value in docker_build_args(role, lock).items()):
            raise ValueError('Docker defaults differ from reviewed lock: ' + role)
    return lock_digest(lock)


def validate_expected_versions(values):
    if not isinstance(values, dict) or set(values) - {'python', 'ruby', 'cocoapods'}:
        raise ValueError('Expected versions support only Python, Ruby and CocoaPods')
    if any(not isinstance(value, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', value) for value in values.values()):
        raise ValueError('Expected versions must be exact three-part versions')
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('check')
    version = commands.add_parser('version')
    version.add_argument('component')
    build = commands.add_parser('build-args')
    build.add_argument('role', choices=['controller', 'android', 'ohos'])
    probe = commands.add_parser('preflight')
    probe.add_argument('role', choices=['controller', 'android', 'ios', 'ohos', 'pr'])
    for field in ('sdk-root', 'clt-root', 'jenkins-war', 'remoting-jar', 'remoting-sha256'):
        probe.add_argument('--' + field)
    probe.add_argument('--java-bin', default='java')
    probe.add_argument('--expected-versions', type=Path, help='管理员每 Agent 精确 Python/Ruby/CocoaPods JSON')
    args = parser.parse_args()
    try:
        if args.command == 'version':
            print(locked_version(args.component))
        elif args.command == 'build-args':
            print(json.dumps(docker_build_args(args.role), sort_keys=True))
        elif args.command == 'preflight':
            print(json.dumps(preflight(args.role, args.sdk_root, args.clt_root, args.java_bin, args.jenkins_war,
                                       args.remoting_jar, args.remoting_sha256,
                                       json.loads(args.expected_versions.read_text(), object_pairs_hook=_unique_object) if args.expected_versions else None),
                             ensure_ascii=False, indent=2))
        else:
            lock = load_toolchain_lock()
            check_repository_pins(lock)
            print(json.dumps({'lockSha256': lock_digest(lock), 'components': len(lock['components'])}))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, 'Toolchain validation failed: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
