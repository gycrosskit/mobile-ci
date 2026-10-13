#!/usr/bin/env python3
"""Linux 鸿蒙指定设备 HAP 构建、验签及蒲公英分发；不维护供应商后台材料。"""

import argparse
from contextlib import nullcontext
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import zipfile

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project import SOURCE_ROOT, platform_config, app_path, harmony_contract
from signing import harmony_provisioning as provisioning
from delivery import dingtalk_notify, pgyer_upload
from delivery.delivery import ArtifactInfo, DeliveryContextError, artifact_sha256, delivery_operation, delivery_namespace, delivery_receipt, resolve_git_target, write_json_object

ROOT = SOURCE_ROOT
MEMORY_OVERHEAD_MB = 4096


class HarmonyReleaseError(RuntimeError):
    pass


def run(command, *, cwd=None, env=None, failure_dir=None):
    # SDK 失败输出可能带签名参数或设备；只返回给校验器，不转印原始日志。
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode:
        if failure_dir is not None:
            failure_dir.mkdir(parents=True, exist_ok=True)
            if failure_dir.is_symlink():
                raise HarmonyReleaseError('诊断目录不能是符号链接')
            # 构建依赖错误供管理员排查，原始内容不进入控制台或归档白名单。
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='build-failure-',
                                             suffix='.log', dir=failure_dir, delete=False) as log:
                log.write(result.stdout + result.stderr)
        raise HarmonyReleaseError("{} 执行失败（exit={}）；未输出原始材料".format(Path(command[0]).name, result.returncode))
    return result.stdout + result.stderr


def read_json(path):
    return provisioning.read_json(Path(path).read_bytes())


def memory_settings():
    # CLI 默认保留原 Release 堆设置；较小 CI 堆必须同时传给原生准备入口。
    result = {}
    for key, default, minimum, maximum in [('gradle', 8192, 4096, 8192), ('node', 2048, 1024, 2048)]:
        value = os.environ.get('HARMONY_' + key.upper() + '_HEAP_MB', str(default))
        if not re.fullmatch(r'[0-9]+', value) or not minimum <= int(value) <= maximum:
            raise HarmonyReleaseError('HARMONY_{}_HEAP_MB 必须为 {}..{} MiB'.format(key.upper(), minimum, maximum))
        result[key] = int(value)
    # -Xmx 仅限制 Java 堆；LLVM、metaspace、链接器仍需独立余量，不能把堆和 RSS 等同。
    result['required'] = (result['gradle'] + result['node'] + MEMORY_OVERHEAD_MB) * 1024 ** 2
    return result


def memory_budget(proc=Path('/proc'), cgroup=Path('/sys/fs/cgroup')):
    info = (proc / 'meminfo').read_text()
    match = re.search(r'^MemAvailable:\s+(\d+) kB$', info, re.M)
    if not match:
        raise HarmonyReleaseError('无法核实 Linux 可用内存')
    physical = [int(match[1]) * 1024]
    swap_match = re.search(r'^SwapFree:\s+(\d+) kB$', info, re.M)
    swap = [int(swap_match[1]) * 1024 if swap_match else 0]
    combined = [physical[0] + swap[0]]
    found_controller = False
    memberships = (proc / 'self/cgroup').read_text().splitlines()
    for row in memberships:
        _, controllers, path = row.split(':', 2)
        if controllers and 'memory' not in controllers.split(','):
            continue
        base = cgroup / 'memory' if controllers else cgroup
        current = base.joinpath(path.lstrip('/')).resolve()
        # 容器命名空间可能只暴露根限制，同时也检查当前组及所有可见祖先。
        paths = [base]
        if current == base.resolve() or base.resolve() in current.parents:
            paths += [current, *[parent for parent in current.parents if base.resolve() in parent.parents]]
        for directory in set(paths):
            limit = directory / ('memory.limit_in_bytes' if controllers else 'memory.max')
            usage = directory / ('memory.usage_in_bytes' if controllers else 'memory.current')
            if limit.exists():
                found_controller = True
                value = limit.read_text().strip()
                # v1 的 LONG_MAX 页对齐值也表示 unlimited。
                memory_limit = None if value == 'max' or int(value) >= 2 ** 60 else int(value)
                if not usage.exists():
                    raise HarmonyReleaseError('cgroup 内存限制缺少使用量，无法核实预算')
                memory_used = int(usage.read_text().strip())
                if memory_limit is not None:
                    physical.append(max(0, memory_limit - memory_used))
                swap_limit_file = directory / ('memory.memsw.limit_in_bytes' if controllers else 'memory.swap.max')
                swap_usage_file = directory / ('memory.memsw.usage_in_bytes' if controllers else 'memory.swap.current')
                if not swap_limit_file.exists() or not swap_usage_file.exists():
                    # 未暴露 swap 记账时不能借用宿主 SwapFree 放宽容器预算。
                    swap.append(0)
                    continue
                value = swap_limit_file.read_text().strip()
                swap_limit = None if value == 'max' or int(value) >= 2 ** 60 else int(value)
                swap_used = int(swap_usage_file.read_text().strip())
                if controllers:
                    # v1 memsw 是 resident+swap；差额为配置允许的 swap，等额即禁用。
                    swappiness = directory / 'memory.swappiness'
                    if swappiness.exists() and int(swappiness.read_text().strip()) == 0:
                        swap.append(0)
                    if swap_limit is not None:
                        combined.append(max(0, swap_limit - swap_used))
                        allowance = max(0, swap_limit - memory_limit) if memory_limit is not None else 0
                        swap.append(max(0, allowance - max(0, swap_used - memory_used)))
                elif swap_limit is not None:
                    swap.append(max(0, swap_limit - swap_used))
    if not found_controller:
        swap.append(0)
    return min(min(physical) + min(swap), min(combined))


def build_settings(build_type):
    if build_type not in ('Debug', 'QA', 'Release'):
        raise HarmonyReleaseError('buildType 必须为 Debug、QA 或 Release')
    return {'buildType': build_type, 'buildMode': 'debug' if build_type == 'Debug' else 'release',
            'debugLogging': build_type != 'Release',
            'defaultApiEnvironment': 'production' if build_type == 'Release' else 'test'}


def plan(config_path, brand, branch, dry_run=True, notify=False, build_type='Release', *, pipeline='test', tag=''):
    if type(notify) is not bool or type(dry_run) is not bool:
        raise HarmonyReleaseError('dry_run 和 notify 必须为 Boolean')
    config = provisioning.load_brand(config_path, brand)
    contract = harmony_contract(brand)
    if contract['adapter'] != 'sxmqlive' and config['bundleName'] != platform_config('harmony').get('appIdentifier'):
        raise HarmonyReleaseError('Harmony bundleName 与可信项目配置不一致')
    if pipeline == 'release' and (branch or build_type != 'Release'):
        raise HarmonyReleaseError('鸿蒙正式入口只接受 Tag 和 Release 构建类型')
    target = resolve_git_target(pipeline, branch, tag, HarmonyReleaseError, brand=brand, tag_infix='-ohos-v')
    ns = config['credentialNamespace']
    return {**config, **build_settings(build_type), 'branch': branch, 'tag': target.ref if pipeline == 'release' else '',
            'expectedVersion': target.version if pipeline == 'release' else None, 'dryRun': dry_run, 'notify': notify, 'uiEngine': contract['uiEngine'],
            'credentials': {'p12': ns + '-release-p12', 'password': ns + '-release-p12-password',
                            'profile': ns + '-' + brand + '-designated-release-profile',
                            'devices': ns + '-' + brand + '-devices-json'}}


def tools():
    clt = os.environ.get('HARMONY_CLT_HOME')
    if not clt:
        raise HarmonyReleaseError('缺少已冻结官方 Linux SDK 的 HARMONY_CLT_HOME')
    clt = Path(clt).resolve()
    paths = {'node': clt / 'tool/node/bin/node', 'hvigor': clt / 'hvigor/bin/hvigorw.js',
             'ohpm': clt / 'bin/ohpm', 'sdk': clt / 'sdk',
             'signer': clt / 'sdk/default/openharmony/toolchains/lib/hap-sign-tool.jar'}
    for key in ('node', 'hvigor', 'ohpm', 'signer'):
        if not paths[key].is_file():
            raise HarmonyReleaseError('完整 CLT 缺少 ' + key)
    return paths


def runtime_preflight(paths):
    sdk = paths['sdk'] / 'default'
    libraries = ['libatomic.so.1', 'libxml2.so.2', 'libGL.so.1',
                 str(sdk / 'hms/toolchains/lib/libimage_transcoder_shared.so')]
    # 厂商依赖只在隔离加载探针中解析，不改变 Gradle/Hvigor 或签名 Java 的搜索路径。
    env = {**os.environ, 'LD_LIBRARY_PATH': os.pathsep.join([
        str(sdk / 'openharmony/previewer/common/bin'), str(sdk / 'hms/toolchains/lib')])}
    probe = '''import ctypes, json, sys
failed = []
for index, library in enumerate(sys.argv[1:]):
    try:
        ctypes.CDLL(library)
    except OSError:
        failed.append(index)
print(json.dumps(failed))
'''
    try:
        failed = json.loads(run([sys.executable, '-c', probe, *libraries], env=env))
    except (HarmonyReleaseError, ValueError):
        raise HarmonyReleaseError('SDK运行库预检失败：隔离加载探针无法完成；未输出原始加载错误') from None
    if not isinstance(failed, list) or any(type(index) is not int or not 0 <= index < len(libraries) for index in failed):
        raise HarmonyReleaseError('SDK运行库预检失败：隔离加载探针结果无效')
    if failed:
        labels = ['libatomic.so.1', 'libxml2.so.2', 'libGL.so.1', 'SDK libimage_transcoder_shared.so']
        raise HarmonyReleaseError('SDK运行库预检失败：' + '、'.join(labels[index] for index in failed) + ' 缺失、依赖未满足或 ELF 不兼容')

    konan = Path(os.environ.get('KONAN_DATA_DIR', str(Path.home() / '.konan')))
    llvm = konan / 'dependencies/cpf-llvm-19-x86_64-linux-dev-20'
    if llvm.exists():
        for name, marker in [('clang++', 'clang version 19.'), ('ld.lld', 'LLD 19.')]:
            try:
                version = run([str(llvm / 'bin' / name), '--version'])
            except (HarmonyReleaseError, OSError):
                raise HarmonyReleaseError('LLVM运行库预检失败：' + name + ' 不能启动；检查已缓存冻结工具链的宿主依赖') from None
            if marker not in version:
                raise HarmonyReleaseError('LLVM运行库预检失败：' + name + ' 不是冻结 LLVM 19 工具链')


def preflight(config):
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise HarmonyReleaseError('当前 K/N 发行版要求 Linux x86_64 构建宿主')
    if not config['enabled']:
        raise HarmonyReleaseError('品牌 HarmonyOS signing.enabled=false；先由维护入口完成材料验收')
    settings = memory_settings()
    budget = memory_budget()
    if budget < settings['required']:
        raise HarmonyReleaseError('可用物理内存 + cgroup 允许的剩余 swap 仅 {:.2f} GiB，不足 {:.2f} GiB（Gradle {} + Node {} + LLVM/原生余量 {} MiB）；宿主 SwapFree 同时受 memory/memsw 限额约束，禁用或未核实的 swap 不计入'.format(
            budget / 1024 ** 3, settings['required'] / 1024 ** 3, settings['gradle'], settings['node'], MEMORY_OVERHEAD_MB))
    paths = tools()
    for binary in ('java', 'openssl', 'bash', 'python3', 'curl', 'unzip', 'shasum'):
        if not shutil.which(binary):
            raise HarmonyReleaseError('构建节点缺少 ' + binary)
    if not re.search(r'version "21[.\"]', run(['java', '-version'])):
        raise HarmonyReleaseError('CLT 26 与当前 CI 组合须使用 JDK 21')
    clt = paths['sdk'].parent
    for relative, expected in [('hvigor/hvigor/package.json', '6.26.8'),
                               ('hvigor/hvigor-ohos-plugin/package.json', '6.26.8'),
                               ('ohpm/package.json', '26.0.0.630')]:
        if read_json(clt / relative).get('version') != expected:
            raise HarmonyReleaseError('工具版本不符合冻结 CLT 26.0.0.851')
    for component in ('native', 'ets', 'toolchains'):
        sdk = read_json(paths['sdk'] / 'default/openharmony' / component / 'oh-uni-package.json')
        if sdk.get('apiVersion') != '26' or sdk.get('version') != '26.0.0.105':
            raise HarmonyReleaseError('SDK 组件版本不符合冻结 CLT 26.0.0.851')
    if run([str(paths['node']), '--version']).strip() != 'v24.14.1':
        raise HarmonyReleaseError('冻结 CLT 26.0.0.851 要求 Node 24.14.1')
    runtime_preflight(paths)
    return paths


@contextmanager
def temporary_json(path, transform, decoder=json.loads):
    original = path.read_bytes()
    permissions = path.stat().st_mode
    try:
        value = transform(decoder(original))
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        yield
    finally:
        path.write_bytes(original)
        path.chmod(permissions)


def hap_metadata(hap, config):
    contract = harmony_contract(config['brand'])
    if contract['adapter'] != 'sxmqlive' and config['bundleName'] != platform_config('harmony').get('appIdentifier'):
        raise HarmonyReleaseError('Harmony bundleName 与可信项目配置不一致')
    if hap.suffix != '.hap' or not hap.is_file():
        raise HarmonyReleaseError('仅接受单个实际 HAP；不分发 APP 容器')
    with zipfile.ZipFile(hap) as archive:
        if archive.namelist().count('module.json') != 1:
            raise HarmonyReleaseError('HAP 必须包含唯一 module.json')
        if any(name.lower().endswith(('.hap', '.app')) for name in archive.namelist()):
            raise HarmonyReleaseError('仅允许一个 HAP 产物，禁止嵌套分发包')
        module = provisioning.read_json(archive.read('module.json'))
        metadata_path = 'resources/resfile/ci-build.json' if contract['adapter'] == 'sxmqlive' else contract['metadata']
        if metadata_path:
            if archive.namelist().count(metadata_path) != 1:
                raise HarmonyReleaseError('HAP 必须包含唯一构建配置 ' + metadata_path)
            packaged_settings = provisioning.read_json(archive.read(metadata_path))
        else:
            packaged_settings = None
    if (not isinstance(module, dict) or not isinstance(module.get('app'), dict)
            or not isinstance(module.get('module'), dict)):
        raise HarmonyReleaseError('HAP module.json 应包含 app 和 module 对象')
    app = module['app']
    entry = module['module']
    if app.get('bundleName') != config['bundleName'] or entry.get('name') != contract['moduleName'] or entry.get('type') != 'entry':
        raise HarmonyReleaseError('HAP 包名或 entry 身份与品牌不匹配')
    if config.get('expectedVersion') and app.get('versionName') != config['expectedVersion']:
        raise HarmonyReleaseError('HAP versionName 与正式 Tag 版本不一致')
    settings = build_settings(config['buildType'])
    if (type(app.get('debug')) is not bool or app['debug'] != (settings['buildMode'] == 'debug')
            or contract['adapter'] == 'sxmqlive' and app.get('buildMode') != settings['buildMode']):
        raise HarmonyReleaseError('HAP 实际 Debug 编译标志与 buildType 不一致')
    if metadata_path and (packaged_settings != settings or type(packaged_settings.get('debugLogging')) is not bool):
        raise HarmonyReleaseError('HAP 构建类型、诊断开关或默认 API 环境元数据不一致')
    return {'bundleName': app['bundleName'], 'versionName': app.get('versionName'), 'versionCode': app.get('versionCode'),
            **settings}


def build(root, config):
    paths = preflight(config)
    settings = memory_settings()
    contract = harmony_contract(config['brand'])
    app = root / contract['app']
    brand = config['brand']
    hap = root / contract['artifact']
    output = hap.parent
    if output.exists():
        shutil.rmtree(output)
    env = {**os.environ, 'CI': 'true', 'DEVECO_SDK_HOME': str(paths['sdk']),
           'DEVECO_NODE_HOME': str(paths['node'].parents[1]), 'OHPM_BIN': str(paths['ohpm']),
           'HARMONY_GRADLE_HEAP_MB': str(settings['gradle']), 'HARMONY_NODE_HEAP_MB': str(settings['node']),
           'NODE_OPTIONS': '--max-old-space-size=' + str(settings['node']), 'OHOS_BUILD_MODE': config['buildMode'],
           'PATH': str(paths['node'].parent) + os.pathsep + os.environ.get('PATH', '')}
    diagnostics = root / 'build/ci-harmony-diagnostics'
    if contract['adapter'] == 'sxmqlive':
        run(['bash', 'install_release_hars.sh'], cwd=app, env=env, failure_dir=diagnostics)

    def unsigned(profile):
        profile['app']['signingConfigs'] = []
        for product in profile['app']['products']:
            product.pop('signingConfig', None)
        return profile

    def constrained(hvigor):
        hvigor['execution'] = {**hvigor.get('execution', {}), 'daemon': False, 'parallel': False}
        hvigor['nodeOptions'] = {**hvigor.get('nodeOptions', {}), 'maxOldSpaceSize': settings['node']}
        return hvigor

    # 密码不进入工作树；无签名构建结束或异常均恢复原配置。
    def decode_hvigor(_):
        module = paths['hvigor'].parents[1] / 'hvigor-ohos-plugin/node_modules/json5'
        return json.loads(run([str(paths['node']), '-e',
                              'console.log(JSON.stringify(require(process.argv[1]).parse(require("fs").readFileSync(process.argv[2],"utf8"))))',
                              str(module), str(app / 'hvigor/hvigor-config.json5')], env=env))

    with temporary_json(app / 'build-profile.json5', unsigned), temporary_json(app / 'hvigor/hvigor-config.json5', constrained, decode_hvigor):
        run([str(paths['node']), '--max-old-space-size=' + str(settings['node']), str(paths['hvigor']), '--mode', 'module',
             '-p', 'module=' + contract['moduleName'] + '@' + contract['product'], '-p', 'product=' + contract['product'], '-p', 'buildMode=' + config['buildMode'],
             '-p', 'buildType=' + config['buildType'],
             '-p', 'requiredDeviceType=phone', 'assembleHap', '--no-parallel', '--no-daemon', '--incremental'],
            cwd=app, env=env, failure_dir=diagnostics)
    if list(output.glob('*.hap')) != [hap]:
        raise HarmonyReleaseError('所选品牌输出必须为唯一 unsigned HAP')
    hap_metadata(hap, config)
    if contract['adapter'] == 'sxmqlive':
        run([str(paths['node']), 'tests/assets-contract.test.cjs', brand, str(hap)], cwd=app, env=env)
    return hap


def java_signer(paths, arguments, temporary):
    # Java @argfile 将密码留在权限 0600 临时文件，不放进进程命令行或 Jenkins 日志。
    values = ['-jar', str(paths['signer']), *arguments]
    if any('\n' in value or '\r' in value for value in values):
        raise HarmonyReleaseError('签名参数不能含换行')
    args = temporary / 'java.args'
    args.write_text('\n'.join('"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"' for value in values))
    args.chmod(0o600)
    try:
        return run(['java', '@' + str(args)])
    finally:
        args.unlink(missing_ok=True)


def materials(temporary):
    result = {}
    if temporary.is_symlink() or not temporary.is_dir() or temporary.stat().st_mode & 0o777 != 0o700:
        raise HarmonyReleaseError('签名材料副本目录必须为私有 0700 临时目录')
    for key, env_name, filename in [('p12', 'HARMONY_P12_FILE', 'release.p12'),
                                    ('profile', 'HARMONY_PROFILE_FILE', 'designated-release.p7b'),
                                    ('devices', 'HARMONY_DEVICES_FILE', 'devices.json')]:
        value = os.environ.get(env_name)
        if not value or not Path(value).is_file() or Path(value).stat().st_size == 0:
            raise HarmonyReleaseError('缺少只读签名材料 ' + env_name)
        source = Path(value).resolve()
        expected = hashlib.sha256(source.read_bytes()).digest()
        # Jenkins SecretFile 绑定路径可能以 .tmp 结尾，SDK 按扩展名判断类型。
        target = temporary / filename
        shutil.copyfile(source, target)
        target.chmod(0o600)
        if hashlib.sha256(target.read_bytes()).digest() != expected:
            raise HarmonyReleaseError('临时签名材料副本与绑定源不一致；停止签名')
        result[key] = target
    if not os.environ.get('HARMONY_P12_PASSWORD'):
        raise HarmonyReleaseError('缺少 HARMONY_P12_PASSWORD')
    cert = run(['openssl', 'pkcs12', '-in', str(result['p12']), '-nokeys', '-clcerts',
                '-passin', 'env:HARMONY_P12_PASSWORD'])
    certificate = provisioning.certificate_pem(cert.encode())
    full_chain = run(['openssl', 'pkcs12', '-in', str(result['p12']), '-nokeys',
                      '-passin', 'env:HARMONY_P12_PASSWORD'])
    chain = ordered_chain(certificate, provisioning.certificate_chain(full_chain.encode()))
    result['certificate'] = certificate
    result['cert'] = temporary / 'release.pem'
    result['cert'].write_bytes(chain)
    result['cert'].chmod(0o600)
    result['deviceRows'] = read_json(result['devices'])
    rows = result['deviceRows']
    if (not isinstance(rows, list) or not rows or len(rows) > 4000
            or any(not isinstance(row, dict) or not isinstance(row.get('udid'), str)
                   or not row['udid'] or str(row.get('deviceType')) != '4' for row in rows)
            or len({row['udid'] for row in rows}) != len(rows)):
        raise HarmonyReleaseError('指定设备签名需完整非空设备 roster；未输出设备材料')
    return result


def certificate_der(certificate):
    return provisioning.openssl('x509', '-outform', 'DER', data=certificate)


def ordered_chain(leaf, chain):
    blocks = re.findall(br'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', chain, re.S)
    if len(blocks) < 2:
        raise HarmonyReleaseError('P12 缺少完整发布证书链；需维护入口补齐官方链后再签包')
    remaining = [block for block in blocks if certificate_der(block) != certificate_der(leaf)]
    if len(remaining) != len(blocks) - 1:
        raise HarmonyReleaseError('P12 发布证书链缺少唯一叶子证书')
    ordered = [leaf]
    while remaining:
        issuer = provisioning.openssl('x509', '-issuer', '-nameopt', 'RFC2253', '-noout', data=ordered[-1]).split(b'=', 1)[-1].strip()
        matches = [block for block in remaining if provisioning.openssl('x509', '-subject', '-nameopt', 'RFC2253', '-noout', data=block).split(b'=', 1)[-1].strip() == issuer]
        if len(matches) != 1:
            raise HarmonyReleaseError('P12 发布证书链不连续或颁发者不唯一')
        ordered.append(matches[0])
        remaining.remove(matches[0])
    root = ordered[-1]
    issuer = provisioning.openssl('x509', '-issuer', '-nameopt', 'RFC2253', '-noout', data=root).split(b'=', 1)[-1].strip()
    subject = provisioning.openssl('x509', '-subject', '-nameopt', 'RFC2253', '-noout', data=root).split(b'=', 1)[-1].strip()
    if issuer != subject:
        raise HarmonyReleaseError('P12 发布证书链未包含根证书')
    return b'\n'.join(ordered) + b'\n'


def verify(hap, config, paths, material, temporary):
    metadata = hap_metadata(hap, config)
    chain = temporary / 'embedded.cer'
    profile = temporary / 'embedded.p7b'
    chain.unlink(missing_ok=True)
    profile.unlink(missing_ok=True)
    output = java_signer(paths, ['verify-app', '-inFile', str(hap), '-outCertChain', str(chain), '-outProfile', str(profile)], temporary)
    if not re.search(r'\bverify-app success\s*$', output, re.M) or not chain.is_file() or not profile.is_file():
        raise HarmonyReleaseError('SDK verify-app 缺少明确成功结果或新鲜证书/Profile')
    try:
        # SDK 导出可能是 root-first；固定预期叶子排序并比较整链，不能只找任意一个成员。
        embedded_chain = ordered_chain(material['certificate'], provisioning.certificate_chain(chain.read_bytes()))
        p12_chain = ordered_chain(material['certificate'], provisioning.certificate_chain(material['cert'].read_bytes()))
    except (HarmonyReleaseError, provisioning.ProvisioningError):
        raise HarmonyReleaseError('HAP 签名证书链与团队 P12 不匹配或不完整') from None
    if embedded_chain != p12_chain:
        raise HarmonyReleaseError('HAP 完整签名证书链与团队 P12 不匹配')
    if profile.read_bytes() != material['profile'].read_bytes():
        raise HarmonyReleaseError('HAP 嵌入 Profile 与所选品牌批准材料不一致')
    result_file = temporary / 'profile-result.json'
    result_file.unlink(missing_ok=True)
    java_signer(paths, ['verify-profile', '-inFile', str(profile), '-outFile', str(result_file)], temporary)
    result = read_json(result_file)
    if result.get('verifiedPassed') is not True:
        raise HarmonyReleaseError('SDK Profile verifiedPassed 必须为严格 true')
    decoded = provisioning.validate_profile(profile.read_bytes(), config, material['certificate'], material['deviceRows'])
    if result.get('content') != decoded:
        raise HarmonyReleaseError('SDK Profile 内容与 CMS 解码结果不一致')
    return {**metadata, 'brand': config['brand'], 'teamKey': config['teamKey'], 'appVerified': True,
            'profileVerified': True, 'deviceCount': len(material['deviceRows']),
            'certificateSha256': hashlib.sha256(certificate_der(material['certificate'])).hexdigest(),
            'sha256': hashlib.sha256(hap.read_bytes()).hexdigest(), 'dryRun': config.get('dryRun', True), 'distributed': False}


def sign(hap, destination, config):
    paths = tools()
    hap_metadata(hap, config)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='harmony-sign-') as directory:
        temporary = Path(directory)
        material = materials(temporary)
        provisioning.validate_profile(material['profile'].read_bytes(), config, material['certificate'], material['deviceRows'])
        signed = temporary / 'signed.hap'
        java_signer(paths, ['sign-app', '-mode', 'localSign', '-keyAlias', 'release', '-signAlg', 'SHA256withECDSA',
                           '-appCertFile', str(material['cert']), '-profileFile', str(material['profile']), '-profileSigned', '1',
                           '-inFile', str(hap), '-keystoreFile', str(material['p12']), '-outFile', str(signed),
                           '-keyPwd', os.environ['HARMONY_P12_PASSWORD'], '-keystorePwd', os.environ['HARMONY_P12_PASSWORD'],
                           '-signCode', '1'], temporary)
        report = verify(signed, config, paths, material, temporary)
        shutil.copyfile(signed, destination)
    return report


def upload(hap, output_dir, config, config_path):
    if not config['enabled']:
        raise HarmonyReleaseError('品牌 HarmonyOS signing.enabled=false')
    report_path = output_dir / 'signature-report.json'
    release_path = output_dir / 'release-info.json'
    # 重新验签绑定本次 Team 材料，不能仅凭上次报告把私钥送到另一个应用。
    with tempfile.TemporaryDirectory(prefix='harmony-upload-') as directory:
        temporary = Path(directory)
        material = materials(temporary)
        report = verify(hap, config, tools(), material, temporary)
        report.update(commit=config.get('commit', ''), branch=config['branch'], tag=config['tag'], uiEngine=config.get('uiEngine', 'kuikly'))
        scope = {'operation': 'harmony-distribution', 'platform': 'harmonyos', 'brand': config['brand']}
        identity = {**scope, 'ui_engine': config.get('uiEngine', 'kuikly'), 'environment': config['buildType'],
                    'commit': report['commit'], 'branch': config['tag'] or config['branch'],
                    'artifact_sha256': report['sha256']}
        if not config['dryRun']:
            identity['certificate_sha256'] = artifact_sha256(material['p12'])
        namespace, legacy = delivery_namespace('pgyer')
        operation = nullcontext(({}, None)) if config['dryRun'] else delivery_operation(identity, scope=scope, namespace=namespace, legacy_compatible=legacy)
        with operation as (_state, save):
            output_dir.mkdir(parents=True, exist_ok=True)
            # dry-run 不覆盖既有真实回执；重新验签失败也保留上次恢复线索。
            persist = not config['dryRun'] or not (report_path.exists() or release_path.exists())
            if persist:
                write_json_object(report_path, report)
            brand = read_json(config_path)[config['brand']]
            release = ArtifactInfo(name=brand.get('displayName', config['brand']), version=str(report['versionName']),
                                   version_code=str(report['versionCode']), package_name=report['bundleName'],
                                   build_key='DRY-RUN', download_url='', qr_code_url='', ui_engine=config.get('uiEngine', 'kuikly'))
            def record(distributed):
                value = {**release.to_dict(), 'platform': 'harmonyos', 'brand': config['brand'],
                         'buildType': config['buildType'], 'sha256': report['sha256'],
                         'commit': report['commit'], 'branch': report['branch'], 'tag': report['tag'],
                         'dryRun': config['dryRun'], 'distributed': distributed}
                if persist:
                    write_json_object(release_path, value)
                return value
            if config['dryRun']:
                return record(False)
            save(phase='pending')
            api_key = os.environ.get('PGYER_API_KEY', '')
            release = pgyer_upload.upload_artifact(
                str(hap), api_key, '{} {} {}'.format(config['brand'], config['buildType'], config['tag'] or config['branch']),
                platform='harmonyos', delivery_identity={'platform': 'harmonyos', 'brand': config['brand'],
                    'ui_engine': config.get('uiEngine', 'kuikly'), 'environment': config['buildType'], 'commit': report['commit'],
                    'branch': config['tag'] or config['branch']})._replace(ui_engine=config.get('uiEngine', 'kuikly'))
            record(False)
            if (release.package_name != report['bundleName'] or release.version != str(report['versionName'])
                    or release.version_code != str(report['versionCode'])):
                raise HarmonyReleaseError('蒲公英解析的包名或版本与已验签 HAP 不一致；未上传 P12')
            if hashlib.sha256(hap.read_bytes()).hexdigest() != report['sha256']:
                raise HarmonyReleaseError('HAP 在验签后改变；未上传 P12')
            pgyer_upload.upload_harmony_certificate(api_key, release.build_key, material['p12'], os.environ['HARMONY_P12_PASSWORD'])
            report['distributed'] = True
            write_json_object(report_path, report)
            result = record(True)
            save(phase='complete', build_key=release.build_key, receipt=release.to_dict())
            return result


def notify(hap, output_dir, config, config_path, *, changelog='', builder='jenkins'):
    if type(config.get('notify')) is not bool or type(config.get('dryRun')) is not bool:
        raise HarmonyReleaseError('dryRun 和 notify 必须为 Boolean')
    if not config['notify'] or config['dryRun']:
        return {'brand': config['brand'], 'platform': 'HarmonyOS', 'notified': False}
    if not config['enabled'] or not re.fullmatch(r'[0-9a-f]{40}', config.get('commit', '')):
        raise HarmonyReleaseError('通知必须使用已启用品牌和明确的 40 位应用 Commit')
    info = read_json(output_dir / 'release-info.json')
    report = read_json(output_dir / 'signature-report.json')
    if not isinstance(info, dict) or not isinstance(report, dict):
        raise HarmonyReleaseError('通知报告必须为 JSON 对象')
    metadata = hap_metadata(hap, config)
    if (not isinstance(metadata['versionName'], str) or not metadata['versionName'].strip()
            or type(metadata['versionCode']) is not int or metadata['versionCode'] <= 0):
        raise HarmonyReleaseError('通知 HAP 必须包含有效版本和构建号')
    sha256 = hashlib.sha256(hap.read_bytes()).hexdigest()
    for value in (info, report):
        if value.get('distributed') is not True or value.get('dryRun') is not False:
            raise HarmonyReleaseError('仅允许 HAP 和安装清单证书已完成实际分发的通知')
        if any(value.get(key) != config[key] for key in ('brand', 'buildType', 'commit', 'branch', 'tag')):
            raise HarmonyReleaseError('通知报告与本次品牌、包类型或应用 Git 来源不一致')
        if value.get('sha256') != sha256:
            raise HarmonyReleaseError('通知报告 SHA-256 与本次 HAP 不一致')
    if (info.get('platform') != 'harmonyos' or info.get('ui_engine') != config.get('uiEngine', 'kuikly')
            or report.get('uiEngine') != config.get('uiEngine', 'kuikly')
            or report.get('appVerified') is not True or report.get('profileVerified') is not True
            or any(report.get(key) != value or type(report.get(key)) is not type(value)
                   for key, value in metadata.items())):
        raise HarmonyReleaseError('通知报告缺少有效鸿蒙 Kuikly 签名或包元数据')
    if any(info.get(key) != str(metadata[field]) for key, field in (
            ('package_name', 'bundleName'), ('version', 'versionName'), ('version_code', 'versionCode'))):
        raise HarmonyReleaseError('蒲公英通知包名或版本与本次已验签 HAP 不一致')
    if not isinstance(info.get('build_key'), str) or not isinstance(info.get('download_url'), str):
        raise HarmonyReleaseError('通知必须包含有效蒲公英构建键和 HTTPS 下载地址')
    url = urlsplit(info['download_url'])
    if (not re.fullmatch(r'[A-Za-z0-9_-]+', info['build_key']) or info['build_key'] == 'DRY-RUN'
            or url.scheme != 'https' or url.hostname not in ('www.pgyer.com', 'pgyer.com')
            or url.username is not None or url.password is not None or url.port not in (None, 443)
            or not re.fullmatch(r'/[A-Za-z0-9_-]+', url.path) or url.query or url.fragment
            or any(character.isspace() for character in info['download_url'])):
        raise HarmonyReleaseError('通知必须包含有效蒲公英构建键和 HTTPS 下载地址')
    artifact = ArtifactInfo.from_dict(info)
    state = delivery_receipt({'operation': 'harmony-distribution', 'platform': 'harmonyos', 'brand': config['brand']})
    identity = state['identity']
    expected = {'artifact_sha256': sha256, 'commit': config['commit'], 'branch': config['tag'] or config['branch'],
                'ui_engine': config.get('uiEngine', 'kuikly'), 'environment': config['buildType']}
    certificate = delivery_receipt({'operation': 'pgyer-certificate', 'build_key': info['build_key']})
    if (state.get('phase') != 'complete' or state.get('build_key') != info['build_key']
            or any(identity.get(key) != value for key, value in expected.items())
            or state.get('receipt') != artifact.to_dict()
            or certificate.get('phase') != 'completed'
            or not re.fullmatch(r'[0-9a-f]{64}', identity.get('certificate_sha256', ''))
            or certificate.get('certificate_sha256') != identity['certificate_sha256']):
        raise HarmonyReleaseError('持久分发或证书回执与本次 HAP 不一致；停止通知')
    brand = dingtalk_notify.load_brand_config(config_path, config['brand'])
    if not config['dryRun']:
        if not brand.testers or any(not tester.mobile for tester in brand.testers):
            raise HarmonyReleaseError('品牌测试人员手机号 Credential 不完整，未发送通知')
        dingtalk_notify.send_release_notification(
            release=artifact, brand=brand, changelog=changelog,
            environment='release' if config['buildType'] == 'Release' else 'test',
            branch=config['tag'] or config['branch'], commit=config['commit'], builder=builder,
            webhook=os.environ.get('DINGTALK_WEBHOOK', ''), secret=os.environ.get('DINGTALK_SECRET', ''),
            platform='HarmonyOS', distribution='pgyer', ui_engine=config.get('uiEngine', 'kuikly'), artifact_sha256=sha256)
    return {'brand': config['brand'], 'platform': 'HarmonyOS', 'ui_engine': config.get('uiEngine', 'kuikly'), 'notified': not config['dryRun']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'preflight', 'build', 'sign', 'verify', 'upload', 'notify'))
    parser.add_argument('--brand', required=True)
    parser.add_argument('--branch', default='')
    parser.add_argument('--pipeline', choices=('test', 'release'), default='test')
    parser.add_argument('--tag', default='')
    parser.add_argument('--build-type', choices=('Debug', 'QA', 'Release'), default='Release')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--config', type=Path, default=ROOT / 'ci/brands.json')
    parser.add_argument('--hap', type=Path)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'build/ci-harmony')
    parser.add_argument('--dry-run', choices=('true', 'false'), default='true')
    parser.add_argument('--notify', choices=('true', 'false'), default='false')
    parser.add_argument('--commit', default='')
    parser.add_argument('--changelog', default='')
    parser.add_argument('--builder', default='jenkins')
    args = parser.parse_args(argv)
    try:
        config = plan(args.config, args.brand, args.branch, args.dry_run == 'true', args.notify == 'true', args.build_type,
                      pipeline=args.pipeline, tag=args.tag)
        if args.command == 'plan':
            report = config
        elif args.command == 'preflight':
            preflight(config)
            settings = memory_settings()
            report = {'brand': args.brand, 'preflight': True, 'gradleHeapMiB': settings['gradle'],
                      'nodeHeapMiB': settings['node'], 'requiredMemoryMiB': settings['required'] // 1024 ** 2}
        elif args.command == 'build':
            report = {'brand': args.brand, 'unsignedHap': str(build(args.root.resolve(), config))}
        elif args.command in ('upload', 'notify'):
            if not args.hap:
                raise HarmonyReleaseError('upload/notify 必须显式指定 --hap')
            commit = run(['git', '-C', str(args.root.resolve()), 'rev-parse', '--verify', 'HEAD']).strip()
            if not re.fullmatch(r'[0-9a-f]{40}', commit) or (args.commit and args.commit != commit):
                raise HarmonyReleaseError('应用 HEAD 与指定 Commit 不一致')
            if args.command == 'notify' and not args.commit:
                raise HarmonyReleaseError('notify 必须显式指定本次应用 --commit')
            config['commit'] = commit
            if args.command == 'upload':
                report = upload(args.hap.resolve(), args.output_dir.resolve(), config, args.config)
            else:
                report = notify(args.hap.resolve(), args.output_dir.resolve(), config, args.config,
                                changelog=args.changelog, builder=args.builder)
        else:
            if not config['enabled']:
                raise HarmonyReleaseError('品牌 HarmonyOS signing.enabled=false')
            if not args.hap:
                raise HarmonyReleaseError('sign/verify 必须显式指定 --hap')
            hap = args.hap.resolve()
            (args.output_dir / 'signature-report.json').unlink(missing_ok=True)
            if args.command == 'sign':
                report = sign(hap, args.output_dir / (args.brand + '-' + args.build_type + '-signed.hap'), config)
            else:
                with tempfile.TemporaryDirectory(prefix='harmony-verify-') as directory:
                    temporary = Path(directory)
                    report = verify(hap, config, tools(), materials(temporary), temporary)
            args.output_dir.mkdir(parents=True, exist_ok=True)
            (args.output_dir / 'signature-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (HarmonyReleaseError, provisioning.ProvisioningError, pgyer_upload.PgyerError, dingtalk_notify.DingTalkError,
            OSError, ValueError, TypeError, zipfile.BadZipFile) as error:
        # 不把外部命令或不可信 JSON 内容拼到日志。
        if isinstance(error, dingtalk_notify.DingTalkError):
            message = '钉钉通知失败；保留交付状态，先核对未知结果，禁止盲目重发'
        elif isinstance(error, pgyer_upload.PgyerError):
            message = '蒲公英分发失败；保留交付状态，先核对未知结果，禁止盲目重传'
        elif isinstance(error, DeliveryContextError):
            message = str(error)
        else:
            message = str(error) if isinstance(error, (HarmonyReleaseError, provisioning.ProvisioningError)) else '材料或配置格式无效；未输出原始内容'
        print(message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
