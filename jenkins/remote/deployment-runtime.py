#!/usr/bin/env python3
"""新实例运行入口；复用严格清单，固定read-only checks不打印工具stderr。"""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deployment_config import load_deployment, private_file, render_compose, require, verify_profiles, unique_keys
from project_config import _read_static_json


def run(command):
    try:
        r = subprocess.run(command, capture_output=True, timeout=60)
        require(r.returncode == 0, '部署运行预检失败；工具原始日志不输出')
        return r.stdout.decode('utf-8')
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as e:
        raise ValueError('部署运行预检无法读取工具结果') from e


def select(manifest, filename):
    m = load_deployment(manifest, private=True)
    verify_profiles(m)
    p = private_file(filename)
    require(str(p) == m['controller']['stateRoot'] + '/compose.json', 'Compose必须为实例stateRoot固定render产物')
    raw = _read_static_json(p, object_pairs_hook=unique_keys)
    require(raw == render_compose(m), 'Compose产物与受信实例/固定模板不一致')
    return m, raw


def check_host(m, config):
    host = {'os': platform.system().lower(), 'arch': {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())}
    require(host == m['controller']['host'], '实际宿主OS/arch不匹配Controller配置')
    require(all(a['host'] == host and a['target'] == m['controller']['target'] for a in m['agents'] if a['runtime'] == 'compose'), 'Compose节点必须位于同一实际宿主/容器平台')
    run(['docker', 'compose', 'version'])
    info = json.loads(run(['docker', 'info', '--format', '{{json .}}']))
    expected = m['controller']['target']
    docker_arch = {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64', 'amd64': 'amd64'}.get(info.get('Architecture'))
    require(info.get('OSType') == expected['os'] and docker_arch == expected['arch'], 'Docker VM平台不匹配target')
    nodes = [m['controller']] + [a for a in m['agents'] if a['runtime'] == 'compose']
    require(type(info.get('NCPU')) is int and info['NCPU'] >= sum(n['resources']['cpu'] for n in nodes)
            and type(info.get('MemTotal')) is int and info['MemTotal'] >= sum(n['resources']['memoryGiB'] for n in nodes) * 1024**3, 'Docker VM CPU/内存不足配置预算')
    for n in nodes:
        image = json.loads(run(['docker', 'image', 'inspect', n['imageId']]))[0]
        require(image['Id'] == n['imageId'] and image.get('Os') == n['target']['os'] and image.get('Architecture') == n['target']['arch'], '完整受审镜像ID未安装或OS/arch与target不匹配')
    for service in config['services'].values():
        for volume in service.get('volumes', []):
            if volume['type'] == 'bind':
                p = Path(volume['source'])
                require(p.exists() and p.resolve() == p, '缺少受信只读挂载文件/SDK或路径使用symlink')


def verify(m, config, compose):
    controller_id = None
    actuals = {}
    for sid in [m['controller']['id']] + [a['id'] for a in m['agents'] if a['runtime'] == 'compose']:
        cid = run(compose + ['ps', '--quiet', sid]).strip()
        require(bool(cid) and '\n' not in cid, '运行服务必须唯一')
        actual = json.loads(run(['docker', 'inspect', cid]))[0]
        actuals[sid] = actual
        desired = config['services'][sid]
        require(actual['State'].get('Running') and actual['Image'] == desired['image'], '容器未运行或完整镜像ID漂移')
        require(actual['Config'].get('User') == '1000:1000', '运行用户漂移')
        require(actual['HostConfig'].get('Memory') == int(desired['mem_limit'][:-1]) * 1024**3
                and actual['HostConfig'].get('NanoCpus') == int(desired['cpus']) * 10**9, '容器CPU/内存预算漂移')
        env = dict(item.split('=', 1) for item in actual['Config']['Env'] if '=' in item)
        require(all(env.get(k) == str(v) for k, v in desired['environment'].items()), '节点环境配置漂移')
        mounts = {x['Destination']: x for x in actual['Mounts']}
        require(set(mounts) == {x['target'] for x in desired['volumes']}, '容器挂载集合漂移或有额外材料')
        for v in desired['volumes']:
            a = mounts[v['target']]
            require(a['Type'] == v['type'] and a['RW'] == (not v.get('read_only', False)), '挂载类型/权限漂移')
            require((a.get('Name') == config['volumes'][v['source']]['name']) if v['type'] == 'volume' else (a.get('Source') == v['source']), '挂载来源漂移')
        if sid == m['controller']['id']:
            require(actual['State'].get('Health', {}).get('Status') == 'healthy', 'Controller不健康')
            controller_id = cid
    for a in m['agents']:
        xml = run(['docker', 'exec', controller_id, 'cat', m['controller']['service']['home'] + '/nodes/' + a['name'] + '/config.xml'])
        node = ET.fromstring(xml)
        require(node.findtext('numExecutors') == '1' and node.findtext('mode') == 'EXCLUSIVE'
                and node.findtext('remoteFS') == a['paths']['work'] and set(node.findtext('label', '').split()) == set(a['labels']), 'Jenkins node配置漂移')
    print('Compose identity/resources/mounts/env and Jenkins node configuration verified read-only. Native launchd, online connection, SDK probes and actual build acceptance remain separate.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['context', 'check', 'verify'])
    parser.add_argument('manifest'); parser.add_argument('compose_file')
    args = parser.parse_args()
    try:
        m, config = select(args.manifest, args.compose_file)
        if args.action == 'context':
            print(' '.join([m['controller']['id']] + [a['id'] for a in m['agents'] if a['runtime'] == 'compose']))
        elif args.action == 'check':
            check_host(m, config)
        else:
            verify(m, config, ['docker', 'compose', '--project-directory', str(Path(__file__).parent), '-f', args.compose_file])
        return 0
    except (ValueError, OSError, KeyError, TypeError, ET.ParseError):
        print('Deployment rejected: private instance, trusted profile, generated configuration or runtime verification failed.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
