#!/usr/bin/env python3
"""管理员受管 Job 的 REST plan/apply/verify；不触发构建，不自动删除遗漏配置。"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from project_config import ROLE_PIPELINES, load_project, validate_project
from jenkins import sync_jenkins_jobs as packaging
from jenkins import sync_jenkins_admin_jobs as admin

Error = packaging.JenkinsJobSyncError
MARKER = re.compile(r'\n?\[mobile-ci-owner:([a-z][a-z0-9-]*):([a-z_]+):([^\]\n]+)\]')
APPROVAL_READBACK = """import groovy.json.JsonOutput
import org.jenkinsci.plugins.scriptsecurity.scripts.ScriptApproval
println(JsonOutput.toJson([approvedScriptHashes: ScriptApproval.get().getApprovedScriptHashes().toList()]))
"""


def digest(source):
    return hashlib.sha256(source.encode()).hexdigest() if source is not None else None


def job_path(name):
    if not isinstance(name, str) or not name or any(not packaging.JOB_NAME_PATTERN.fullmatch(part) for part in name.split('/')):
        raise Error('无效精确 Job 全名')
    return ''.join('job/' + urllib.parse.quote(part, safe='') + '/' for part in name.split('/'))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Error('Jenkins API 拒绝重定向')


class JenkinsAPI:
    def __init__(self, url, user, token):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1')):
            raise Error('Jenkins URL 必须为 HTTPS 或本机 HTTP，不能嵌入凭据')
        if not user or not token or ':' in user or any(c in user + token for c in '\r\n'):
            raise Error('必须通过环境变量提供 Jenkins 用户与 API token')
        self.url = url.rstrip('/') + '/'
        self.authorization = 'Basic ' + base64.b64encode((user + ':' + token).encode()).decode()
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, source=None, missing=False, content_type='application/xml; charset=utf-8', method=None):
        request = urllib.request.Request(self.url + path, data=source.encode() if source is not None else None,
            headers={'Authorization': self.authorization, 'Content-Type': content_type},
            method=method or ('POST' if source is not None else 'GET'))
        try:
            with self.opener.open(request, timeout=30) as response:
                if response.status not in (200, 201, 204):
                    raise Error('Jenkins API 返回未知状态；停止且不重试')
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise Error('Jenkins API 响应超出限制')
                return body.decode('utf-8')
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing and source is None:
                return None
            raise Error('Jenkins API HTTP {}；停止且不重试'.format(error.code)) from None
        except (urllib.error.URLError, TimeoutError, UnicodeError):
            raise Error('Jenkins API 请求失败；结果可能未知，先 verify，不重试写入') from None

    def config(self, name):
        return self.request(job_path(name) + 'config.xml', missing=True)

    def delete(self, name):
        # Jenkins Item 的标准 DELETE 返回 204；doDelete 的 UI 302 不适合拒绝重定向的客户端。
        self.request(job_path(name), method='DELETE')

    def approved_scripts(self):
        # ScriptApproval 没有导出的只读 REST；固定脚本仅回读哈希，不接受用户代码、不赋予权限。
        response = self.request('scriptText', urllib.parse.urlencode({'script': APPROVAL_READBACK}),
                                content_type='application/x-www-form-urlencoded')
        try:
            value = json.loads(response)
        except ValueError:
            raise Error('精确脚本审批回读无效，保持禁用') from None
        return value.get('approvedScriptHashes') if isinstance(value, dict) else None

    def info(self, name):
        return self.json(job_path(name) + 'api/json?tree=fullName,buildable,nextBuildNumber,upstreamProjects[fullName],downstreamProjects[fullName]')

    def json(self, path):
        try:
            value = json.loads(self.request(path))
        except (TypeError, ValueError):
            raise Error('Jenkins API JSON 无效') from None
        if not isinstance(value, dict):
            raise Error('Jenkins API JSON 必须是对象')
        return value

    def idle(self):
        queue = self.json('queue/api/json?tree=items[id]')
        computers = self.json('computer/api/json?tree=computer[executors[currentExecutable[url]],oneOffExecutors[currentExecutable[url]]]')
        if not isinstance(queue.get('items'), list) or queue['items']:
            raise Error('构建队列非空或不可验证')
        if not isinstance(computers.get('computer'), list):
            raise Error('无法验证运行中构建')
        for computer in computers['computer']:
            for key in ('executors', 'oneOffExecutors'):
                if not isinstance(computer.get(key), list) or any(executor.get('currentExecutable') is not None for executor in computer[key]):
                    raise Error('构建仍在执行或运行状态未知')

    def inventory(self):
        result = {}
        def visit(prefix=''):
            response = self.json((job_path(prefix) if prefix else '') + 'api/json?tree=jobs[name,fullName,_class]')
            if not isinstance(response.get('jobs'), list):
                raise Error('删除引用清单不完整')
            for item in response['jobs']:
                name = item.get('fullName')
                expected = prefix + '/' + item.get('name', '') if prefix else item.get('name')
                if name != expected or name in result or not isinstance(item.get('_class'), str):
                    raise Error('删除引用清单身份不一致')
                job_path(name)
                if item['_class'] == 'com.cloudbees.hudson.plugins.folder.Folder':
                    visit(name)
                else:
                    source = self.config(name)
                    if source is None:
                        raise Error('删除引用清单读取时发生变化')
                    result[name] = source
        visit()
        return result


def owner(source):
    root = packaging._parse_xml(source, 'ownership')
    if root.tag != 'flow-definition':
        raise Error('只管理受信 Pipeline Job')
    matches = MARKER.findall(root.findtext('description') or '')
    if len(matches) > 1:
        raise Error('Job ownership 重复')
    return matches[0] if matches else None


def mark(source, project, role, name):
    root = packaging._parse_xml(source, name)
    description = root.find('description')
    if description is None:
        description = ET.SubElement(root, 'description')
    description.text = MARKER.sub('', description.text or '') + '\n[mobile-ci-owner:{}:{}:{}]'.format(project, role, name)
    return ET.tostring(root, encoding='unicode')


def normalized(source):
    root = packaging._parse_xml(source, 'config')
    description = root.find('description')
    if description is not None:
        root.remove(description)
    return packaging._normalize_config(ET.tostring(root, encoding='unicode'), 'config')


def desired_configs(profile):
    """沿用唯一模板和已有 XML/审批验证，不接受 plan 提供 Pipeline。"""
    configs = {}
    for spec, source in packaging.render_all(profile=profile).items():
        role = next(role for role, job in profile['jobs'].items() if job['name'] == spec.full_name)
        configs[spec.full_name] = (role, spec, source)
    for spec, source in admin.render_all(profile=profile).items():
        name = spec.folder + '/' + spec.name if spec.folder else spec.name
        configs[name] = (spec.role, spec, source)
    return configs


def equivalent(role, spec, desired, current):
    if role in ('android_test', 'android_release', 'ios_test', 'ios_release', 'harmony_test', 'harmony_release'):
        return normalized(desired) == normalized(packaging._restore_omitted_scm_browsers(desired, current, 'config'))
    return normalized(admin._normalize_admin_source(spec, desired)) == normalized(admin._normalize_admin_source(spec, current))


def with_disabled(source, disabled):
    root = packaging._parse_xml(source, 'config')
    node = root.find('disabled')
    if node is None:
        node = ET.SubElement(root, 'disabled')
    node.text = 'true' if disabled else 'false'
    return ET.tostring(root, encoding='unicode')


def disabled(source):
    return packaging._parse_xml(source, 'config').findtext('disabled', 'false') == 'true'


def assert_script_approvals(api, source):
    if disabled(source):
        return
    root = packaging._parse_xml(source, 'script approvals')
    hashes = set()
    for node in root.iter():
        if node.findtext('sandbox') == 'false' and node.find('script') is not None:
            script = node.findtext('script') or ''
            hashes.add('SHA512:' + hashlib.sha512(('groovy:' + script).encode()).hexdigest())
    if not hashes:
        return
    approved = api.approved_scripts()
    if not isinstance(approved, list) or any(not isinstance(item, str) for item in approved) or not hashes <= set(approved):
        raise Error('无法回读全部精确脚本审批；保持禁用并按受控审批部署流程处理')


def assert_deletable(api, name, source, inventory):
    info = api.info(name)
    if (info.get('fullName') != name or type(info.get('nextBuildNumber')) is not int or info['nextBuildNumber'] != 1
            or info.get('upstreamProjects', []) or info.get('downstreamProjects', []) or not disabled(source)):
        raise Error('删除只允许已禁用、从未构建且无上下游引用的受管 Job；有历史须另行归档')
    for other, config in inventory.items():
        if other == name:
            continue
        root = packaging._parse_xml(config, other)
        # 可执行内容能通过插件或反射调度，不能靠关键字证明没有消费者。
        definition = root.find('definition')
        if definition is not None and (definition.find('scriptPath') is not None or (definition.findtext('script') or '').strip()):
            raise Error('存在不可验证的动态 Pipeline 引用，拒绝删除')
        if any(list(root.find(tag)) for tag in ('builders', 'publishers', 'buildWrappers', 'triggers') if root.find(tag) is not None):
            raise Error('存在不可验证的插件执行或触发配置，拒绝删除')
        if root.tag not in ('flow-definition', 'project'):
            raise Error('存在未知类型 Job，无法证明无引用')
        if name in config or name.rpartition('/')[2] in config:
            raise Error('Job 配置仍含删除目标引用')


def make_plan(api, profile, delete_jobs=(), adopt_jobs=()):
    validate_project(profile)
    if profile.get('scm', {}).get('provider', 'gitee') != 'gitee':
        raise Error('Jenkins 生命周期仅支持受信 Gitee 应用项目')
    if set(delete_jobs) & {job['name'] for job in profile['jobs'].values()}:
        raise Error('删除目标须先从可信配置移除；遗漏配置不会自动删除')
    desired = desired_configs(profile)
    if set(adopt_jobs) - set(desired):
        raise Error('认领目标不属于当前受信配置')
    inventory = api.inventory() if delete_jobs else {}
    actions = []
    for name, (role, spec, source) in desired.items():
        current = api.config(name)
        admin.assert_signing_authorization(spec, current) if isinstance(spec, admin.AdminJobSpec) else None
        expected_owner = (profile['id'], role, name)
        if current is not None:
            actual_owner = owner(current)
            if actual_owner != expected_owner:
                if actual_owner is not None or name not in adopt_jobs:
                    raise Error('现存 Job 未明确认领或归属其他项目：' + name)
                # 管理员显式认领仍须匹配受信 Pipeline、SCM 和全部受管配置；ACL/启停保持。
                candidate = packaging.preserve_job_authorization(source, current, name)
                if not equivalent(role, spec, candidate, current):
                    raise Error('认领 Job 与受信模板不一致：' + name)
            source = packaging.preserve_job_authorization(source, current, name)
        else:
            folder = name.rpartition('/')[0]
            if folder:
                folder_source = api.config(folder)
                if folder_source is None or packaging._parse_xml(folder_source, folder).tag != 'com.cloudbees.hudson.plugins.folder.Folder':
                    raise Error('必须先按权限规划创建 Folder：' + folder)
        if current is not None:
            root = packaging._parse_xml(source, name)
            previous = packaging._parse_xml(current, name).find('description')
            description = root.find('description')
            if description is not None:
                root.remove(description)
            if previous is not None:
                root.append(previous)
            source = ET.tostring(root, encoding='unicode')
        if 'enabled' in profile['jobs'][role]:
            source = with_disabled(source, not profile['jobs'][role]['enabled'])
        elif current is None:
            source = with_disabled(source, True)
        source = mark(source, *expected_owner)
        assert_script_approvals(api, source)
        same = current is not None and owner(current) == expected_owner and equivalent(role, spec, source, current) and disabled(source) == disabled(current)
        if not same:
            actions.append({'job': name, 'role': role, 'operation': 'create' if current is None else 'update',
                            'before': digest(current), 'after': digest(source), 'xml': source})
    for name in sorted(set(delete_jobs)):
        job_path(name)
        source = api.config(name)
        if source is None:
            continue
        binding = owner(source)
        if binding is None or binding != (profile['id'], binding[1], name) or binding[1] not in ROLE_PIPELINES:
            raise Error('删除目标不是本项目受管 Job：' + name)
        assert_deletable(api, name, source, inventory)
        actions.append({'job': name, 'role': binding[1], 'operation': 'delete', 'before': digest(source), 'after': None})
    return {'version': 1, 'controller': api.url, 'project': profile['id'], 'profileSha256': digest(json.dumps(profile, sort_keys=True)),
            'ciCommit': os.environ.get('CI_AUTOMATION_COMMIT', ''), 'adoptJobs': sorted(set(adopt_jobs)), 'deleteJobs': sorted(set(delete_jobs)),
            'inventory': {name: digest(xml) for name, xml in sorted(inventory.items())}, 'actions': actions}


def private_write(path, source):
    path = Path(path)
    # 管理员显式提供的目录仍不能跟随 symlink 或放宽已有目录权限。
    parent = path.parent
    if any(part.is_symlink() for part in (parent, *parent.parents)):
        raise Error('plan/备份路径不能包含符号链接')
    for directory in reversed((parent, *parent.parents)):
        if not directory.exists():
            directory.mkdir(mode=0o700)
    if parent.stat().st_mode & 0o077 or parent.stat().st_uid != os.geteuid():
        raise Error('plan/备份目录必须为 0700')
    descriptor, temporary = tempfile.mkstemp(prefix='.lifecycle-', dir=parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
    finally:
        os.unlink(temporary)


def private_read(path, limit):
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise Error('私有输入路径不能包含符号链接')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'r') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_mode & 0o077
                or metadata.st_uid != os.geteuid() or metadata.st_size > limit):
            raise Error('私有输入必须为本用户的 0600 有界单链接普通文件')
        source = stream.read(limit + 1)
        if len(source) > limit:
            raise Error('私有输入超出大小限制')
        return source


def apply_plan(api, profile, plan, backup_dir, confirm_delete=()):
    if profile.get('testOnly') is True:
        raise Error('testOnly 配置拒绝部署')
    deletions = sorted(action['job'] for action in plan['actions'] if action['operation'] == 'delete')
    if sorted(set(confirm_delete)) != deletions:
        raise Error('必须确认 plan 中的精确删除全名清单')
    fresh = make_plan(api, profile, plan['deleteJobs'], plan['adoptJobs'])
    if fresh != plan:
        raise Error('plan 或线上配置已变化；重新 plan，拒绝覆盖')
    if not plan['actions']:
        return
    api.idle()
    controller = api.json('api/json?tree=quietingDown')
    if type(controller.get('quietingDown')) is not bool:
        raise Error('Controller quietDown 状态不可验证')
    record = Path(backup_dir) / digest(json.dumps(plan, sort_keys=True))
    before = {action['job']: api.config(action['job']) for action in plan['actions']}
    if any(digest(before[action['job']]) != action['before'] for action in plan['actions']):
        raise Error('写入前 Job 配置发生变化，拒绝覆盖')
    # 先证明恢复记录可持久保存，再停止调度；路径错误不能把健康实例留在维护模式。
    private_write(record / 'before.json', json.dumps({'plan': plan, 'configs': before}, ensure_ascii=False, indent=2))
    acquired = not controller['quietingDown']
    if acquired:
        api.request('quietDown', '')
    # 失败时保留 quietDown 和回执，让管理员 verify/恢复，不能启动潜在未知结果的任务。
    api.idle()
    if api.json('api/json?tree=quietingDown').get('quietingDown') is not True:
        raise Error('Controller 未进入 quietDown，拒绝写入')
    for action in plan['actions']:
        name = action['job']
        api.idle()
        if digest(api.config(name)) != action['before']:
            raise Error('写入前 Job 配置发生变化，拒绝覆盖')
        if action['operation'] != 'delete':
            assert_script_approvals(api, action['xml'])
        if action['operation'] == 'create':
            folder, _, leaf = name.rpartition('/')
            api.request((job_path(folder) if folder else '') + 'createItem?name=' + urllib.parse.quote(leaf, safe=''), action['xml'])
        elif action['operation'] == 'update':
            api.request(job_path(name) + 'config.xml', action['xml'])
        else:
            assert_deletable(api, name, before[name], api.inventory())
            api.delete(name)
        verify_action(api, profile, action)
    private_write(record / 'verified.json', json.dumps({'project': profile['id'], 'actions': [{'job': a['job'], 'operation': a['operation']} for a in plan['actions']]}, indent=2))
    if acquired:
        api.request('cancelQuietDown', '')


def verify_action(api, profile, action):
    current = api.config(action['job'])
    if action['operation'] == 'delete':
        if current is not None:
            raise Error('删除回读仍存在 Job')
        return
    if current is None or owner(current) != (profile['id'], action['role'], action['job']):
        raise Error('Job 回读身份不一致')
    role, spec, trusted = desired_configs(profile)[action['job']]
    if role != action['role'] or digest(action['xml']) != action['after']:
        raise Error('验证计划的角色或配置摘要不一致')
    # 回读还必须满足当前受信模板，不能只比较可同时被修改的线上 XML 和私有计划。
    if isinstance(spec, admin.AdminJobSpec):
        admin.assert_signing_authorization(spec, current)
    trusted = packaging.preserve_job_authorization(trusted, current, action['job'])
    if 'enabled' in profile['jobs'][role]:
        trusted = with_disabled(trusted, not profile['jobs'][role]['enabled'])
    if not equivalent(role, spec, trusted, current) or disabled(trusted) != disabled(current):
        raise Error('Job 不符合当前受信模板或启停声明')
    if (not equivalent(role, spec, action['xml'], current) or disabled(action['xml']) != disabled(current)
            or packaging._parse_xml(action['xml'], 'desired').findtext('description') != packaging._parse_xml(current, 'current').findtext('description')):
        raise Error('Job 回读配置不一致；保留备份和 quietDown')
    assert_script_approvals(api, current)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'apply', 'verify'))
    parser.add_argument('--project', required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True, help='本用户 0600 API token 文件；不接受命令行 token')
    parser.add_argument('--backup-dir', type=Path)
    parser.add_argument('--delete-job', action='append', default=[])
    parser.add_argument('--adopt-job', action='append', default=[])
    parser.add_argument('--confirm-delete', action='append', default=[])
    args = parser.parse_args(argv)
    try:
        api = JenkinsAPI(args.url, os.environ.get('JENKINS_USER', ''), private_read(args.token_file, 4096).strip())
        profile = load_project(args.project)
        if args.command == 'plan':
            plan = make_plan(api, profile, args.delete_job, args.adopt_job)
            private_write(args.plan, json.dumps(plan, ensure_ascii=False, indent=2))
        else:
            plan = json.loads(private_read(args.plan, 8 * 1024 * 1024))
            if (plan.get('controller') != api.url or plan.get('project') != profile['id']
                    or plan.get('profileSha256') != digest(json.dumps(profile, sort_keys=True))
                    or plan.get('ciCommit') != os.environ.get('CI_AUTOMATION_COMMIT', '')):
                raise Error('plan 的 Controller/项目/配置/CI SHA 不匹配')
            if args.command == 'apply':
                if args.backup_dir is None:
                    raise Error('apply 必须指定私有 --backup-dir')
                apply_plan(api, profile, plan, args.backup_dir, args.confirm_delete)
            else:
                for action in plan['actions']:
                    verify_action(api, profile, action)
        print(json.dumps({'project': profile['id'], 'actions': [{'job': a['job'], 'operation': a['operation']} for a in plan['actions']]}, ensure_ascii=False))
        return 0
    except (Error, ValueError, KeyError, OSError) as error:
        print('Job 生命周期失败：' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
