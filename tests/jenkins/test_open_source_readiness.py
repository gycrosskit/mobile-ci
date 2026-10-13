"""公开配置可离线渲染；示例标记不能跨过生产部署与静态文件边界。"""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import project_config
from jenkins import sync_jenkins_jobs as packaging
from jenkins import sync_jenkins_admin_jobs as admin

ROOT = Path(__file__).resolve().parents[2]


def public_profile():
    return project_config.validate_project(json.loads((ROOT / "examples/second-project/project.json").read_text()))


class OpenSourceReadinessTest(unittest.TestCase):
    def test_static_json_rejects_mutable_aliases_nonregular_and_oversized_files(self):
        for kind in ("writable", "hardlink", "symlink", "parent-symlink", "fifo", "oversized"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                source = root / "trusted.json"
                source.write_text('{"marker":"fixed"}')
                source.chmod(0o600)
                target = source
                if kind == "writable":
                    source.chmod(0o666)
                elif kind == "hardlink":
                    os.link(source, root / "alias.json")
                elif kind == "symlink":
                    target = root / "alias.json"; target.symlink_to(source)
                elif kind == "parent-symlink":
                    linked = root / "linked"; linked.symlink_to(root, target_is_directory=True)
                    target = linked / source.name
                elif kind == "fifo":
                    source.unlink(); os.mkfifo(source, 0o600)
                elif kind == "oversized":
                    source.write_bytes(b" " * 65537)
                with self.assertRaises(project_config.ProjectConfigError):
                    project_config._read_static_json(target)

    def test_core_renders_public_jobs_without_any_company_profile(self):
        profile = public_profile()
        with tempfile.TemporaryDirectory() as directory, patch.object(project_config, "PROJECTS_DIR", Path(directory)), \
                patch.object(project_config, "load_project", side_effect=AssertionError("renderer must not load company profile")), \
                patch.dict(os.environ, CI_AUTOMATION_COMMIT="a" * 40):
            packaged = packaging.render_all(profile=profile)
            managed = admin.render_all(profile)
            self.assertEqual(2, len(packaged))
            self.assertEqual(2, len(managed))
            for spec, text in {**packaged, **managed}.items():
                config = ET.fromstring(text)
                self.assertIn(profile["credentials"]["scm"], text)
                self.assertNotIn("shanxi-hanning-network/", text)
                self.assertNotIn("gitee-ssh", text)
                script = config.findtext("definition/script")
                if script is None:
                    self.assertEqual("a" * 40, config.findtext("definition/scm/branches/hudson.plugins.git.BranchSpec/name"))
                else:
                    self.assertIn("a" * 40, script)

    def test_both_apply_commands_reject_testonly_before_render_or_controller_io(self):
        profile = public_profile()
        for module, operation in ((packaging, "apply_configs"), (admin, "apply")):
            with self.subTest(module=module.__name__), patch.object(project_config, "load_project", return_value=profile), \
                    patch.object(module, "render_all") as render, patch.object(module, operation) as apply, \
                    patch.object(packaging, "_docker") as docker, contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(1, module.main(["apply", "--project", profile["id"]]))
                self.assertIn("testOnly", errors.getvalue())
                render.assert_not_called(); apply.assert_not_called(); docker.assert_not_called()

    def test_production_profiles_keep_existing_apply_path(self):
        for marker in (None, False):
            profile = copy.deepcopy(public_profile())
            if marker is None:
                profile.pop("testOnly")
            else:
                profile["testOnly"] = marker
            for module, operation in ((packaging, "apply_configs"), (admin, "apply")):
                with self.subTest(marker=marker, module=module.__name__), \
                        patch.object(project_config, "load_project", return_value=profile), \
                        patch.object(module, "render_all", return_value={}) as render, \
                        patch.object(module, operation) as apply:
                    self.assertEqual(0, module.main(["apply", "--project", profile["id"]]))
                    render.assert_called_once(); apply.assert_called_once()


if __name__ == "__main__":
    unittest.main()
