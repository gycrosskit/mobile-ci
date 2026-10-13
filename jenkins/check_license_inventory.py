#!/usr/bin/env python3
"""核对源码发行的许可证据；不把运行依赖清单当成镜像分发授权。"""
import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def check(root=ROOT, scope='source'):
    inventory = json.loads((root / 'docs/audits/third-party-inventory.json').read_text())
    if inventory.get('version') != 1 or inventory.get('binaryReleaseApproved') is not False:
        raise ValueError('未知清单或缺少镜像发行阻断策略')
    declared = dict(line.split(':', 1) for line in (root / 'jenkins/controller/plugins.txt').read_text().splitlines()
                    if line and not line.startswith('#'))
    plugins = inventory['plugins']
    if len({entry['id'] for entry in plugins}) != len(plugins):
        raise ValueError('插件清单有重复项')
    pinned = {entry['id']: entry['version'] for entry in plugins if entry.get('direct') is True}
    if declared != pinned:
        raise ValueError('固定插件已变化，必须重新核查实际依赖许可')
    for entry in plugins:
        if (not entry.get('licenses') or not entry['pom'].startswith('https://repo.jenkins-ci.org/releases/')
                or not re.fullmatch('[0-9a-f]{64}', entry.get('pomSha256', ''))):
            raise ValueError('插件缺少官方固定版本许可证据')
        if any(not item.get('name') or not item.get('url') for item in entry['licenses']):
            raise ValueError('插件许可无法定位')
    for entry in inventory['fastlaneRuntime']:
        if not entry.get('licenses'):
            raise ValueError('Gem 许可未核实')
    fastlane = [entry for entry in inventory['fastlaneRuntime'] if entry['name'] == 'fastlane']
    if len(fastlane) != 1 or fastlane[0]['version'] != (root / 'delivery/.fastlane-version').read_text().strip():
        raise ValueError('Fastlane 版本变化，必须重核运行依赖')
    for entry in inventory['externalTools']:
        if entry.get('license') not in {'MIT', 'Apache-2.0'} or not re.fullmatch('[0-9a-f]{64}', entry.get('sha256', '')):
            raise ValueError('外部工具许可证据未核实')
    if scope != 'source':
        raise ValueError('镜像/二进制分发未批准：须独立 SBOM、来源、通知及对应源码核查')
    return {'scope': scope, 'plugins': len(plugins), 'directPlugins': len(pinned),
            'fastlaneRuntimeGems': len(inventory['fastlaneRuntime']), 'binaryReleaseApproved': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', choices=('source', 'image'), default='source')
    args = parser.parse_args()
    try:
        print(json.dumps(check(scope=args.scope)))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, str(error) + '\n')
