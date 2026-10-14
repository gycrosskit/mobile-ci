#!/usr/bin/env python3
"""从同一可信 Compose 清单解析灾备资源，不退回另一项目的默认容器或卷。"""
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys


def resources(config, project, *, controller_dr=True):
    backup = project.get('backup', {})
    if controller_dr and (backup.get('scope') != 'controller' or backup.get('recipientAuthority') != 'controller-admin'):
        raise ValueError('全局 Controller 灾备仅接受明确 controller-admin 范围')
    roles = project['runtime']['roles']
    services = config['services']
    if set(roles) != {'controller', 'android', 'pr', 'maintenance'} or any(name not in services for name in roles.values()):
        raise ValueError('灾备需要同一清单内的四个可信角色')
    def service(role):
        return services[roles[role]]
    def resource(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value):
            raise ValueError('灾备资源身份无效')
        return value
    def volume(role, target):
        mounts = [item for item in service(role).get('volumes', []) if item['target'] == target]
        if len(mounts) != 1 or mounts[0]['type'] != 'volume' or mounts[0].get('read_only', False):
            raise ValueError('灾备材料必须位于角色的唯一可写命名卷')
        key = mounts[0]['source']
        return resource(config.get('volumes', {}).get(key, {}).get('name', config['name'] + '_' + key))
    env = service('android').get('environment', {})
    work = env.get('JENKINS_AGENT_WORKDIR')
    receipt = env.get('DELIVERY_STATE_ROOT')
    if not all(isinstance(value, str) and re.fullmatch(r'/[A-Za-z0-9_./-]+', value) for value in (work, receipt)):
        raise ValueError('灾备回执根无效')
    work_path, receipt_path = PurePosixPath(work), PurePosixPath(receipt)
    if ('..' in work_path.parts or '..' in receipt_path.parts or receipt_path.name != 'delivery-state'
            or work_path not in receipt_path.parents or 'workspace' in receipt_path.parts):
        raise ValueError('回执必须位于 Agent work 卷内的独立 delivery-state 根')
    uid = project['runtime']['linuxUid']
    if type(uid) is not int or uid <= 0:
        raise ValueError('灾备恢复必须使用可信非 root Linux owner')
    signing_root = project['signingBackup']['root']
    result = {
        'home': volume('controller', '/var/jenkins_home'),
        'signing': volume('maintenance', signing_root),
        'work': volume('android', work),
        'controller': resource(service('controller').get('container_name')),
        'android': resource(service('android').get('container_name')),
        'maintenance': resource(service('maintenance').get('container_name')),
        'uid': uid, 'receiptRelative': str(receipt_path.relative_to(work_path)),
        'image': service('controller').get('image'),
        'scope': 'controller_dr', 'projectId': project['id'],
    }
    if not isinstance(result['image'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:@-]+', result['image']):
        raise ValueError('灾备必须使用清单固定的 Controller 镜像')
    if len({result[key] for key in ('home', 'signing', 'work')}) != 3:
        raise ValueError('灾备材料/状态卷必须相互隔离')
    if len({result[key] for key in ('controller', 'android', 'maintenance')}) != 3:
        raise ValueError('灾备停止/恢复容器角色必须相互隔离')
    return result


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from project_config import load_project
    project = load_project(os.environ.get('JENKINS_CI_PROJECT', 'sxmqlive'))
    rendered = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
    if not rendered or len(rendered) > 4 * 1024 * 1024:
        raise ValueError('无法读取有界完整 Compose 清单')
    value = resources(json.loads(rendered), project)
    # 只输出已经校验的数据，由 shell read 接收；不输出、拼接或执行清单中的 shell。
    print(' '.join(str(value[key]) for key in ('home', 'signing', 'work', 'controller', 'android',
                                              'maintenance', 'uid', 'receiptRelative', 'image', 'projectId')))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('灾备资源解析失败；检查可信项目配置与完整 Compose 清单，未操作容器或数据卷。')
