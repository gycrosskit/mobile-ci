"""历史扫描必须覆盖孤立提交，不能被配置、例外或扫描器错误绕过。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from jenkins import audit_git_history as audit


class HistoryAuditTest(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory).resolve()
        repo = root / 'repo'; repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL).decode().strip()
        git('init','-q','-b','master'); git('config','user.name','Fixture'); git('config','user.email','fixture@example.invalid')
        (repo/'source.txt').write_text('public code\n')
        git('add','.'); git('commit','-qm','base')
        base = git('rev-parse','HEAD')
        git('checkout','-qb','isolated'); (repo/'source.txt').write_text('orphan secret fixture\n')
        git('add','.'); git('commit','-qm','commit message fixture')
        unreachable = git('rev-parse','HEAD'); git('checkout','-q','master'); git('branch','-D','isolated'); git('reflog','expire','--expire=now','--all')
        scanner = root/'scanner'; scanner.write_text('#!/bin/sh\nprintf "8.30.1\\n"\n'); scanner.chmod(0o700)
        return repo, scanner, base, unreachable

    def test_all_objects_include_unreachable_commits_and_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _, base, unreachable = self.fixture(directory)
            objects,total,commits = audit.inventory(repo)
            self.assertEqual([base], commits)
            self.assertIn(unreachable, objects)
            destination = Path(directory).resolve()/'objects'; destination.mkdir(mode=0o700)
            audit.materialize(repo,objects,destination)
            self.assertIn(b'commit message fixture',(destination/(unreachable+'.commit')).read_bytes())
            self.assertEqual(total,sum(path.stat().st_size for path in destination.iterdir()))

    def test_unknown_object_and_size_budgets_fail_instead_of_skipping(self):
        with tempfile.TemporaryDirectory() as directory:
            repo,_,_,_=self.fixture(directory)
            for field in ('MAX_OBJECTS','MAX_OBJECT_BYTES','MAX_TOTAL_BYTES'):
                with self.subTest(field=field),patch.object(audit,field,0),self.assertRaises(audit.AuditError):
                    audit.inventory(repo)

    def test_shallow_clone_and_missing_scanner_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            repo,scanner,base,_=self.fixture(directory)
            (repo/'.git/shallow').write_text(base+'\n')
            with self.assertRaises(audit.AuditError): audit.audit(repo,scanner)
            with self.assertRaises(OSError): audit.audit(repo,Path(directory).resolve()/'missing')

    def test_approval_requires_exact_rule_single_line_and_source_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); oid='a'*40
            line=b'known public code'; (root/(oid+'.blob')).write_bytes(line+b'\nsecond\n')
            finding={'File':str(root/(oid+'.blob')),'RuleID':'fixture','StartLine':1,'EndLine':1,'Match':'PRIVATE','Secret':'PRIVATE'}
            with patch.object(audit,'APPROVED',{('fixture',1,hashlib.sha256(line).hexdigest())}):
                self.assertTrue(audit.sanitize_finding(finding,'dir',root,{oid:('blob',24)},root)['approved'])
                for change in ({'EndLine':2},{'RuleID':'other'},{'StartLine':2,'EndLine':2}):
                    result=audit.sanitize_finding(dict(finding,**change),'dir',root,{oid:('blob',24)},root)
                    self.assertFalse(result['approved']); self.assertNotIn('PRIVATE',json.dumps(result))
                with self.assertRaises(audit.AuditError): audit.sanitize_finding(dict(finding,File='../escape'),'dir',root,{oid:('blob',24)},root)

    def test_approved_validation_literal_does_not_allow_a_key_body_or_other_multiline_span(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); oid='a'*40; path=root/(oid+'.blob')
            marker=b'-----BEGIN PRIVATE KEY-----'
            path.write_bytes(marker+b'\n'+b'A'*64+b'\n-----END PRIVATE KEY-----\n')
            finding={'File':str(path),'RuleID':'private-key','StartLine':1,'EndLine':3}
            self.assertFalse(audit.sanitize_finding(finding,'dir',root,{oid:('blob',path.stat().st_size)},root)['approved'])
            finding['EndLine']=2
            self.assertFalse(audit.sanitize_finding(finding,'dir',root,{oid:('blob',path.stat().st_size)},root)['approved'])

    def test_configuration_and_allow_comments_cannot_override_scan_and_logs_are_not_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            for name,content in (('default.toml','[extend]\nuseDefault = true\n'),('empty.ignore','')):
                (root/name).write_text(content)
            report=root/'git-report.json'; report.write_text('[]'); report.chmod(0o600)
            with patch.dict(os.environ,GITLEAKS_CONFIG='bad',GITLEAKS_CONFIG_TOML='bad',GIT_DIR='bad'), \
                    patch.object(audit.subprocess,'run',return_value=subprocess.CompletedProcess([],0)) as runner:
                self.assertEqual([],audit.scan(Path('/fixed/scanner'),'git',root,root))
                args=runner.call_args.args[0]; env=runner.call_args.kwargs['env']
                self.assertIn('--ignore-gitleaks-allow',args); self.assertIn('--redact=100',args)
                self.assertIn('--config='+str(root/'default.toml'),args)
                self.assertIn('--gitleaks-ignore-path='+str(root/'empty.ignore'),args)
                self.assertIn('--log-opts=--all --full-history --no-renames --no-ext-diff --no-textconv',args)
                self.assertNotIn('GITLEAKS_CONFIG',env); self.assertNotIn('GITLEAKS_CONFIG_TOML',env); self.assertNotIn('GIT_DIR',env)

    def test_public_fixture_code_approval_requires_exact_two_line_source_and_rule(self):
        digest='9e79164d7fb8b53dfde019f6b34454da129b5d193d3b1dcb083eb5bb08d51d6d'
        lines=Path(__file__).read_bytes().splitlines()
        spans=[b'\n'.join(lines[index:index+2]) for index in range(len(lines)-1)]
        source=next(span for span in spans if hashlib.sha256(span).hexdigest()==digest)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); oid='b'*40; path=root/(oid+'.blob')
            path.write_bytes(source+b'\nextra\n')
            finding={'File':str(path),'RuleID':'private-key','StartLine':1,'EndLine':2}
            objects={oid:('blob',path.stat().st_size)}
            self.assertTrue(audit.sanitize_finding(finding,'dir',root,objects,root)['approved'])
            for change in ({'RuleID':'generic-api-key'},{'EndLine':3},{'EndLine':1}):
                self.assertFalse(audit.sanitize_finding(dict(finding,**change),'dir',root,objects,root)['approved'])
            path.write_bytes(source+b' changed\n')
            self.assertFalse(audit.sanitize_finding(finding,'dir',root,objects,root)['approved'])

    def test_scanner_errors_malformed_and_inconsistent_reports_fail_closed(self):
        for status,content in ((2,'[]'),(1,'[]'),(0,'[{}]'),(0,'{}'),(0,'broken')):
            with self.subTest(status=status,content=content),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve(); (root/'git-report.json').write_text(content)
                with patch.object(audit.subprocess,'run',return_value=subprocess.CompletedProcess([],status)),self.assertRaises((audit.AuditError,ValueError)):
                    audit.scan(Path('/fixed/scanner'),'git',root,root)

    def test_source_manifest_digest_approval_requires_verified_file_and_exact_json_line(self):
        source_root=Path(__file__).resolve().parents[2]
        names=('signing/harmony_jenkins_credentials.py','signing/ios_jenkins_credentials.py',
               'tests/signing/test_harmony_jenkins_credentials.py','tests/signing/test_ios_jenkins_credentials.py')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); oid='c'*40; path=root/(oid+'.blob')
            finding={'File':str(path),'RuleID':'generic-api-key','StartLine':1,'EndLine':1}
            for name in names:
                digest=hashlib.sha256((source_root/name).read_bytes()).hexdigest()
                for indent in (2,4):
                    line=(' '*indent+json.dumps(name)+': '+json.dumps(digest)+',').encode()
                    with self.subTest(name=name,indent=indent):
                        path.write_bytes(line+b'\nextra\n')
                        objects={oid:('blob',path.stat().st_size)}
                        self.assertTrue(audit.sanitize_finding(finding,'dir',root,objects,root)['approved'])
                        for change in ({'RuleID':'private-key'},{'EndLine':2}):
                            self.assertFalse(audit.sanitize_finding(dict(finding,**change),'dir',root,objects,root)['approved'])
                        changed_digest=('0' if digest[0]!='0' else '1')+digest[1:]
                        for changed in (line.replace(digest.encode(),changed_digest.encode()),
                                        line.replace(name.encode(),('other/'+name).encode()), b' '+line,
                                        line+b' changed'):
                            path.write_bytes(changed+b'\n')
                            objects={oid:('blob',path.stat().st_size)}
                            self.assertFalse(audit.sanitize_finding(finding,'dir',root,objects,root)['approved'])

    def test_report_rejects_fifo_symlink_and_hardlink_without_exposing_data(self):
        for kind in ('fifo','symlink','hardlink'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve(); path=root/'report.json'
                if kind=='fifo': os.mkfifo(path,0o600)
                else:
                    original=root/'original'; original.write_text('private'); original.chmod(0o600)
                    if kind=='symlink': path.symlink_to(original)
                    else: os.link(original,path)
                with self.assertRaises(audit.AuditError): audit.write_report(path,{'safe':True})


if __name__=='__main__': unittest.main()
