import os
from unittest.mock import patch
import argparse
import json
import hashlib
import tempfile
import shutil
import unittest
import zipfile
from pathlib import Path

from project import SOURCE_ROOT
from delivery.delivery import ArtifactInfo

from delivery.release import (
    ReleaseError,
    build_parser,
    classify_release_target,
    find_apk,
    gradle_task,
    prepare_release,
    resolve_target_brand,
    validate_apk_version,
    validate_apk_identity,
    upload_release,
    notify_release,
)


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        # 解析器归宿主应用；此回归只使用确定的假品牌验证产物拒绝边界。
        parser = patch("delivery.release.load_android_brand_specs", return_value={"miaoshou": {"applicationId": "com.skill.cure.acu"}})
        parser.start(); self.addCleanup(parser.stop)

    def test_real_delivery_callers_supply_identity_and_hash_and_dry_run_has_no_state(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            config = self._write_config(repo)
            value = json.loads(config.read_text())
            value["_defaults"]["testers"] = [{"name": "fixture", "mobile": "13800000000"}]
            config.write_text(json.dumps(value))
            apk = self._write_apk(repo)
            output = repo / "output"
            prepare_release(build_parser().parse_args([
                "prepare", "--repo", str(repo), "--config", str(config), "--brand", "miaoshou",
                "--pipeline", "test", "--branch", "dev/miaoshou", "--manual-changelog", "- fixture",
                "--commit", "abc123", "--builder", "fixture", "--output-dir", str(output)]))
            args = argparse.Namespace(config=config, context=output / "context.json",
                                      output=output / "release-info.json", release_info=output / "release-info.json",
                                      dry_run=True)
            with patch("delivery.dingtalk_notify.delivery_operation") as operation:
                upload_release(args)
                notify_release(args)
                operation.assert_not_called()
            args.dry_run = False
            release = ArtifactInfo("fixture", "1.2.3", "123", "com.example", "key", "", "")
            with patch("delivery.release.upload_artifact", return_value=release) as upload, \
                    patch("delivery.release.send_release_notification") as notify:
                upload_release(args)
                notify_release(args)
            context = json.loads(args.context.read_text())
            self.assertEqual({key: context[key] for key in
                              ("platform", "brand", "ui_engine", "environment", "commit", "branch")},
                             upload.call_args.kwargs["delivery_identity"])
            self.assertEqual(hashlib.sha256(apk.read_bytes()).hexdigest(),
                             notify.call_args.kwargs["artifact_sha256"])

    @staticmethod
    def _write_config(repo: Path) -> Path:
        config = repo / "brands.json"
        config.write_text(
            json.dumps(
                {
                    "_defaults": {"testers": []},
                    "miaoshou": {
                        "displayName": "妙手中医",
                        "pgyerChannels": {},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return config

    @staticmethod
    def _write_apk(
        repo: Path,
        build_type: str = "qa",
        version: str = "1.2.3",
        filename: str = "app.apk",
        ui_engine: str = "cmp",
    ) -> Path:
        module = "androidKuiklyApp" if ui_engine == "kuikly" else "app"
        apk = repo / module / "build/outputs/apk/miaoshou" / build_type / filename
        apk.parent.mkdir(parents=True)
        with zipfile.ZipFile(str(apk), "w") as archive:
            archive.writestr("assets/ui-engine.txt", ui_engine + "\n")
        plugin_relative = Path("build-logic/src/main/kotlin/com/skillcureacu/buildlogic/BrandConfigurationPlugin.kt")
        plugin = repo / plugin_relative
        plugin.parent.mkdir(parents=True, exist_ok=True)
        plugin.write_text("// Synthetic application brand parser input\n")
        (apk.parent / "output-metadata.json").write_text(
            json.dumps(
                {"applicationId": "com.skill.cure.acu", "variantName": "miaoshou" + build_type.capitalize(),
                 "elements": [{"versionName": version, "outputFile": apk.name, "filters": []}]}
            ),
            encoding="utf-8",
        )
        return apk

    def test_prepare_writes_generic_and_legacy_android_context(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            config = self._write_config(repo)
            apk = self._write_apk(repo)
            output = repo / "output"

            prepare_release(
                argparse.Namespace(
                    repo=repo,
                    config=config,
                    brand="miaoshou",
                    pipeline="test",
                    apk=apk,
                    manual_changelog="- 验证 Android 测试包",
                    last_successful_commit="",
                    branch="feature/miaoshou/delivery-context",
                    tag="",
                    commit="abc123",
                    builder="tester",
                    work_item_type="story",
                    work_item_id="1024",
                    work_item_url="https://tracker.example.invalid/zentao/story-view-1024.html",
                    output_dir=output,
                )
            )

            context = json.loads((output / "context.json").read_text(encoding="utf-8"))
            self.assertEqual("android", context["platform"])
            self.assertEqual("cmp", context["ui_engine"])
            self.assertEqual(str(apk.resolve()), context["artifact_path"])
            self.assertEqual(str(apk.resolve()), context["apk_path"])
            self.assertEqual("pgyer", context["delivery"])
            self.assertEqual("Qa", context["build_type"])
            self.assertEqual("story", context["work_item"]["type"])
            self.assertEqual("1024", context["work_item"]["id"])

    def test_gradle_task_follows_pipeline(self):
        for engine, module in (("cmp", "app"), ("kuikly", "androidKuiklyApp")):
            for pipeline, build in (("test", "Qa"), ("release", "Release")):
                with self.subTest(pipeline=pipeline, ui_engine=engine):
                    self.assertEqual(f":{module}:assembleMiaoshou{build}", gradle_task("miaoshou", pipeline, engine))
        self.assertEqual(":app:assembleMiaoshouQa", gradle_task("miaoshou", "test"))

    def test_ui_engine_defaults_from_environment_and_accepts_explicit_override(self):
        for command in ("validate", "prepare"):
            arguments = [command, "--brand", "miaoshou", "--pipeline", "test"]
            with patch.dict(os.environ, {"CI_UI_ENGINE": "kuikly"}):
                self.assertEqual("kuikly", build_parser().parse_args(arguments).ui_engine)
                self.assertEqual("cmp", build_parser().parse_args(arguments + ["--ui-engine", "cmp"]).ui_engine)

    def test_each_engine_prepares_upload_record_and_notification(self):
        for engine in ("cmp", "kuikly"):
            with self.subTest(ui_engine=engine), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory)
                config = self._write_config(repo)
                self._write_apk(repo, ui_engine=engine)
                output = repo / "output"
                args = build_parser().parse_args([
                    "prepare", "--repo", str(repo), "--config", str(config),
                    "--brand", "miaoshou", "--pipeline", "test", "--branch", "dev/miaoshou",
                    "--ui-engine", engine, "--manual-changelog", "- test", "--commit", "abc123",
                    "--builder", "tester", "--output-dir", str(output),
                ])
                prepare_release(args)
                context_path = output / "context.json"
                self.assertEqual(engine, json.loads(context_path.read_text())["ui_engine"])
                record = output / "release-info.json"
                upload_release(argparse.Namespace(config=config, context=context_path, output=record, dry_run=True))
                self.assertEqual(engine, json.loads(record.read_text())["ui_engine"])
                notify_args = argparse.Namespace(config=config, context=context_path, release_info=record, dry_run=True)
                self.assertEqual(0, notify_release(notify_args))
                value = json.loads(record.read_text())
                value["ui_engine"] = "kuikly" if engine == "cmp" else "cmp"
                record.write_text(json.dumps(value))
                with self.assertRaisesRegex(ReleaseError, "UI 引擎"):
                    notify_release(notify_args)

    def test_apk_identity_rejects_wrong_host_and_invalid_engine_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            cmp_apk = self._write_apk(repo)
            kuikly_apk = self._write_apk(repo, ui_engine="kuikly")
            self.assertEqual(cmp_apk.resolve(), find_apk(repo, "miaoshou", "test", "cmp"))
            self.assertEqual(kuikly_apk.resolve(), find_apk(repo, "miaoshou", "test", "kuikly"))
            for apk, engine in ((cmp_apk, "kuikly"), (kuikly_apk, "cmp")):
                with self.assertRaisesRegex(ReleaseError, "目录"):
                    validate_apk_identity(repo, apk, "miaoshou", "test", engine)
            for marker in (b"kuikly\n", b"unknown\n", b"\xff", None, "not-zip"):
                with self.subTest(marker=marker):
                    if marker == "not-zip":
                        cmp_apk.write_bytes(b"not-zip")
                    else:
                        with zipfile.ZipFile(str(cmp_apk), "w") as archive:
                            if marker is not None:
                                archive.writestr("assets/ui-engine.txt", marker)
                    with self.assertRaisesRegex(ReleaseError, "UI 引擎"):
                        validate_apk_identity(repo, cmp_apk, "miaoshou", "test")

    def test_classifies_supported_test_branches(self):
        cases = (
            ("origin/release/hehekang-1.0.3", "release/hehekang-1.0.3", "1.0.3"),
            ("refs/heads/feature/hehekang/appointment-reminder", "feature/hehekang/appointment-reminder", None),
            ("origin/dev/hehekang", "dev/hehekang", None),
        )
        for branch, expected_ref, expected_version in cases:
            with self.subTest(branch=branch):
                target = classify_release_target("hehekang", "test", branch=branch)
                self.assertEqual(expected_ref, target.ref)
                self.assertEqual(expected_version, target.version)
                self.assertEqual("test", target.environment)

    def test_resolves_brand_from_supported_git_references(self):
        cases = (
            ("test", {"branch": "feature/kangmin/appointment"}),
            ("test", {"branch": "dev/kangmin"}),
            ("test", {"branch": "release/kangmin-1.0.3"}),
            ("release", {"tag": " kangmin-v1.0.3-rc.1 "}),
        )
        for pipeline, reference in cases:
            with self.subTest(pipeline=pipeline, reference=reference):
                self.assertEqual("kangmin", resolve_target_brand(pipeline, **reference))

    def test_brand_version_tag_is_release_environment(self):
        target = classify_release_target(
            "hehekang",
            "release",
            tag="hehekang-v1.0.3",
        )

        self.assertEqual("hehekang-v1.0.3", target.ref)
        self.assertEqual("1.0.3", target.version)
        self.assertEqual("release", target.environment)

    def test_rejects_invalid_pipeline_references(self):
        cases = (
            ({"pipeline": "test", "branch": "release/kangmin-1.0.3"},
             "测试包分支品牌必须为 hehekang，当前分支为：release/kangmin-1.0.3"),
            ({"pipeline": "test", "branch": "master"},
             "测试包分支必须使用 feature/<brand>/<summary>、dev/<brand> 或 "
             "release/<brand>-<version>，当前为：master"),
            ({"pipeline": "test", "branch": "feature/search-result"},
             "测试包分支必须使用 feature/<brand>/<summary>、dev/<brand> 或 "
             "release/<brand>-<version>，当前为：feature/search-result"),
            ({"pipeline": "release", "tag": "kangmin-v1.0.3"},
             "正式发布 Tag 必须使用 hehekang-v<version>，当前为：kangmin-v1.0.3"),
            ({"pipeline": "test", "branch": "release/hehekang-1.0.3", "tag": "hehekang-v1.0.3"},
             "测试包 Job 不接受 Tag：hehekang-v1.0.3"),
            ({"pipeline": "release", "branch": "release/hehekang-1.0.3"},
             "正式发布 Tag 必须使用 <brand>-v<version>，当前为：unknown"),
        )
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ReleaseError) as error:
                    classify_release_target("hehekang", **arguments)
                self.assertEqual(message, str(error.exception))

    def test_brand_resolution_rejects_unknown_pipeline(self):
        with self.assertRaises(ReleaseError) as error:
            resolve_target_brand("unknown", branch="dev/hehekang", tag="hehekang-v1.0.3")
        self.assertEqual("Jenkins Job 类型必须为 test 或 release：unknown", str(error.exception))

    def test_find_apk_uses_only_requested_build_type(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            qa_apk = self._write_apk(repo, filename="custom-output.apk")
            self.assertEqual(qa_apk.resolve(), find_apk(repo, "miaoshou", "test"))

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._write_apk(repo, build_type="release", filename="custom-output.apk")
            with self.assertRaises(ReleaseError):
                find_apk(repo, "miaoshou", "test")

    def test_validate_apk_version_matches_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            apk = self._write_apk(Path(directory), version="1.0.0")
            self.assertEqual("1.0.0", validate_apk_version(apk, "1.0.0"))
            with self.assertRaises(ReleaseError):
                validate_apk_version(apk, "1.0.3")


    def test_apk_identity_rejects_wrong_identity_or_ambiguous_metadata(self):
        cases = (
            ("package", lambda v: v.update(applicationId="com.example.other")),
            ("brand", lambda v: v.update(variantName="kangminQa")),
            ("build-type", lambda v: v.update(variantName="miaoshouRelease")),
            ("multiple", lambda v: v["elements"].append(dict(v["elements"][0]))),
            ("missing-elements", lambda v: v.pop("elements")),
            ("path", lambda v: v["elements"][0].update(outputFile="../other.apk")),
            ("absolute", lambda v: v["elements"][0].update(outputFile="/tmp/other.apk")),
            ("undeclared", lambda v: v["elements"][0].update(outputFile="other.apk")),
            ("split", lambda v: v["elements"][0].update(filters=[{"filterType": "ABI", "value": "arm64-v8a"}])),
            ("missing-filters", lambda v: v["elements"][0].pop("filters")),
        )
        for name, mutate in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory)
                apk = self._write_apk(repo)
                metadata = apk.parent / "output-metadata.json"
                value = json.loads(metadata.read_text(encoding="utf-8"))
                mutate(value)
                metadata.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ReleaseError):
                    validate_apk_identity(repo, apk, "miaoshou", "test")
                with self.assertRaises(ReleaseError):
                    find_apk(repo, "miaoshou", "test")

    def test_apk_identity_rejects_missing_file_and_bad_metadata(self):
        for case in ("missing-apk", "missing-metadata", "invalid-json", "non-object", "non-apk"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory)
                apk = self._write_apk(repo, filename="report.txt" if case == "non-apk" else "app.apk")
                metadata = apk.parent / "output-metadata.json"
                if case == "missing-apk":
                    apk.unlink()
                elif case == "missing-metadata":
                    metadata.unlink()
                elif case != "non-apk":
                    metadata.write_text("{" if case == "invalid-json" else "[]", encoding="utf-8")
                with self.assertRaises(ReleaseError):
                    validate_apk_identity(repo, apk, "miaoshou", "test")

    def test_find_apk_ignores_newer_stale_file_and_never_guesses_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            apk = self._write_apk(repo)
            stale = apk.with_name("stale.apk")
            stale.write_bytes(b"stale")
            os.utime(stale, ns=(apk.stat().st_mtime_ns + 1, apk.stat().st_mtime_ns + 1))
            self.assertEqual(apk.resolve(), find_apk(repo, "miaoshou", "test"))
            destination = repo / "app/build/outputs/apk/other-miaoshou/qa"
            destination.parent.mkdir(parents=True)
            shutil.move(str(apk.parent), destination)
            with self.assertRaises(ReleaseError):
                find_apk(repo, "miaoshou", "test")

    def test_explicit_apk_rejected_before_context_or_changelog_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            config = self._write_config(repo)
            apk = self._write_apk(repo)
            metadata = apk.parent / "output-metadata.json"
            value = json.loads(metadata.read_text(encoding="utf-8"))
            value["variantName"] = "kangminQa"
            metadata.write_text(json.dumps(value), encoding="utf-8")
            output = repo / "output"
            args = build_parser().parse_args([
                "prepare", "--repo", str(repo), "--config", str(config),
                "--brand", "miaoshou", "--pipeline", "test", "--branch", "dev/miaoshou",
                "--apk", str(apk), "--manual-changelog", "- test", "--commit", "abc123",
                "--builder", "tester", "--output-dir", str(output),
                "--ui-engine", "cmp",
            ])
            with patch("delivery.release.generate_changelog") as changelog:
                with self.assertRaisesRegex(ReleaseError, "变体"):
                    prepare_release(args)
            changelog.assert_not_called()
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
