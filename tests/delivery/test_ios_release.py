import argparse
import contextlib
import io
import json
import hashlib
import plistlib
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from delivery.ios_release import (
    IosReleaseError,
    classify_release_target,
    load_ios_brand_config,
    notify_release,
    prepare_release,
    build_parser,
    read_ipa_metadata,
    record_app_store_upload,
    resolve_target_brand,
    upload_test_release,
    validate_ipa,
    validate_ipa_build_settings,
    validate_ipa_ui_engine,
)
from delivery.delivery import ArtifactInfo, DeliveryContextError
from project import SOURCE_ROOT, TOOL_ROOT


class IosReleaseTest(unittest.TestCase):
    def test_real_delivery_callers_supply_identity_and_hash_and_dry_run_has_no_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"TESTER_MOBILE": "13800000000"}):
            config, ipa, output = self._prepare(Path(directory))
            args = argparse.Namespace(config=config, context=output / "context.json",
                                      output=output / "release-info.json", release_info=output / "release-info.json",
                                      dry_run=True)
            with patch("delivery.dingtalk_notify.delivery_operation") as operation:
                upload_test_release(args)
                notify_release(args)
                operation.assert_not_called()
            args.dry_run = False
            release = ArtifactInfo("fixture", "1.2.3", "123", "com.example", "key", "", "")
            with patch("delivery.ios_release.upload_artifact", return_value=release) as upload, \
                    patch("delivery.ios_release.send_release_notification") as notify:
                upload_test_release(args)
                notify_release(args)
            context = json.loads(args.context.read_text())
            self.assertEqual({key: context[key] for key in
                              ("platform", "brand", "ui_engine", "environment", "commit", "branch")},
                             upload.call_args.kwargs["delivery_identity"])
            self.assertEqual(hashlib.sha256(ipa.read_bytes()).hexdigest(),
                             notify.call_args.kwargs["artifact_sha256"])

    def test_current_enabled_test_signing_configs_are_loadable(self):
        repo = SOURCE_ROOT
        config = repo / "ci" / "brands.json"
        values = json.loads(config.read_text(encoding="utf-8"))

        enabled = {
            brand for brand, value in values.items()
            if not brand.startswith("_")
            and value.get("ios", {}).get("signing", {}).get("test", {}).get("enabled")
        }
        self.assertTrue(enabled)
        for brand in enabled:
            with self.subTest(brand=brand):
                signing = load_ios_brand_config(config, brand, "test")
                if signing.signing_source == "repository":
                    signing_dir = repo / signing.repository_signing_path
                    self.assertTrue((signing_dir / "distribution.p12").is_file())
                    self.assertTrue((signing_dir / "distribution-p12.password").is_file())
                else:
                    self.assertEqual("jenkins", signing.signing_source)
                    self.assertTrue(signing.distribution_p12_credential_id.endswith("-distribution-p12"))

    def test_test_signing_source_is_explicit_and_release_always_uses_jenkins(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = self._write_config(Path(temporary))
            value = json.loads(config.read_text())
            for source in ("repository", "jenkins", "arbitrary-file"):
                value["_iosTeams"]["test_owner"]["testSigningSource"] = source
                config.write_text(json.dumps(value))
                if source == "arbitrary-file":
                    with self.assertRaises(IosReleaseError):
                        load_ios_brand_config(config, "kangmin", "test")
                else:
                    ios = load_ios_brand_config(config, "kangmin", "test")
                    self.assertEqual(source, ios.signing_source)
                    self.assertEqual("ios-team-test-owner-distribution-p12", ios.distribution_p12_credential_id)
                self.assertEqual("jenkins", load_ios_brand_config(config, "kangmin", "release").signing_source)

    def test_json_config_matches_each_legacy_field_from_one_brand_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = self._write_config(Path(temporary))
            common = ["config", "--config", str(config), "--brand", "kangmin", "--pipeline", "test"]
            args = build_parser().parse_args(common + ["--format", "json"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                args.action(args)
            values = json.loads(output.getvalue())
            self.assertEqual("dev", values["environment"])
            self.assertEqual("Test Owner AdHoc", values["profile-name"])
            self.assertEqual("TESTTEAM01", values["team-id"])
            for pipeline in ("test", "release"):
                source = (TOOL_ROOT / "jenkins" / ("Jenkinsfile.ios." + pipeline)).read_text()
                requested_fields = set(re.findall(r"iosConfig\['([^']+)'\]", source))
                self.assertTrue(requested_fields)
                self.assertFalse(requested_fields - values.keys(), requested_fields - values.keys())
            for field, expected in values.items():
                args = build_parser().parse_args(common + ["--field", field])
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    args.action(args)
                self.assertEqual(expected, output.getvalue().strip())

    def _write_config(self, directory: Path) -> Path:
        config = directory / "brands.json"
        config.write_text(
            json.dumps(
                {
                    "_defaults": {
                        "testers": [
                            {"name": "测试A", "mobileEnv": "TESTER_MOBILE"}
                        ]
                    },
                    "_iosTeams": {
                        "test_owner": {
                            "teamId": "TESTTEAM01",
                            "credentialNamespace": "ios-team-test-owner",
                            "repositorySigningPath": "signing/ios/test_owner",
                        },
                        "release_owner": {
                            "teamId": "RELTEAM001",
                            "credentialNamespace": "ios-team-release-owner",
                            "repositorySigningPath": "signing/ios/release_owner",
                        },
                    },
                    "miaoshou": {"displayName": "妙手中医"},
                    "kangmin": {
                        "displayName": "康民中医",
                        "ios": {
                            "scheme": "kangmin",
                            "bundleId": "com.example.ios",
                            "build": {
                                "test": {
                                    "environment": "dev",
                                    "xcconfig": "iosApp/Configuration/Kangmin.DevRelease.xcconfig",
                                },
                                "release": {
                                    "environment": "prod",
                                    "distribution": "appstore",
                                    "xcconfig": "iosApp/Configuration/Kangmin.ProdRelease.xcconfig",
                                },
                            },
                            "signing": {
                                "test": {
                                    "enabled": True,
                                    "team": "test_owner",
                                    "profileNames": {
                                        "pgyer": "Test Owner AdHoc",
                                        "testflight": "Test Owner AppStore",
                                    },
                                },
                                "release": {
                                    "enabled": True,
                                    "team": "release_owner",
                                    "profileName": "Release Owner AppStore",
                                },
                            },
                        },
                        "pgyerChannels": {"test": "", "release": ""},
                    },
                }
            ),
            encoding="utf-8",
        )
        return config

    def _write_ipa(
        self,
        directory: Path,
        version: str = "1.2.3",
        pipeline: str = "test",
        ui_engine: str = "cmp",
    ) -> Path:
        ipa = directory / "Kangmin.ipa"
        is_test = pipeline == "test"
        info = {
            "CFBundleIdentifier": "com.example.ios",
            "CFBundleShortVersionString": version,
            "CFBundleVersion": "20260829153000",
            "CFBundleDisplayName": "康民中医",
            "SkillCureBuildType": "release",
            "SkillCureIsDebug": "NO",
            "SkillCureDefaultApiEnvironment": "test" if is_test else "production",
            "SkillCureDebugToolsEnabled": "YES" if is_test else "NO",
            "SkillCureSdkProduction": "NO" if is_test else "YES",
        }
        with zipfile.ZipFile(str(ipa), "w") as archive:
            archive.writestr(
                "Payload/Kangmin.app/Info.plist",
                plistlib.dumps(info, fmt=plistlib.FMT_BINARY),
            )
            if ui_engine == "kuikly":
                archive.writestr(
                    "Payload/Kangmin.app/Frameworks/OpenKuiklyIOSRender.framework/OpenKuiklyIOSRender",
                    b"renderer fixture",
                )
        return ipa

    def _prepare(
        self,
        root: Path,
        pipeline: str = "test",
        channel: str = "",
        with_work_item: bool = False,
        ui_engine: str = "cmp",
    ):
        config = self._write_config(root)
        ipa = self._write_ipa(root, pipeline=pipeline, ui_engine=ui_engine)
        output = root / "output"
        is_release = pipeline == "release"
        prepare_release(
            argparse.Namespace(
                repo=root,
                config=config,
                brand="kangmin",
                pipeline=pipeline,
                ui_engine=ui_engine,
                channel=channel,
                ipa=ipa,
                manual_changelog="- 验证 iOS {}包".format(
                    "正式" if is_release else "测试"
                ),
                last_successful_commit="",
                branch="" if is_release else "feature/kangmin/ios-ci",
                tag="kangmin-ios-v1.2.3" if is_release else "",
                commit="abc123",
                builder="tester",
                work_item_type="bug" if with_work_item else "",
                work_item_id="2048" if with_work_item else "",
                work_item_url=(
                    "https://tracker.example.invalid/zentao/bug-view-2048.html"
                    if with_work_item else ""
                ),
                output_dir=output,
            )
        )
        return config, ipa, output

    def test_ui_engine_parser_uses_environment_and_rejects_unknown_engine(self):
        with patch.dict("os.environ", {"CI_UI_ENGINE": "kuikly"}):
            for command in ("validate", "config", "prepare"):
                arguments = [command, "--brand", "kangmin", "--pipeline", "test"]
                if command == "config":
                    arguments += ["--format", "json"]
                self.assertEqual("kuikly", build_parser().parse_args(arguments).ui_engine)
                self.assertEqual("cmp", build_parser().parse_args(arguments + ["--ui-engine", "cmp"]).ui_engine)
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    build_parser().parse_args(arguments + ["--ui-engine", "unknown"])

    def test_kuikly_uses_brand_scheme_and_existing_environment_and_signing(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = self._write_config(Path(temporary))
            values = json.loads(config.read_text())
            for scheme in ("kangmin", "Miaoshou", "Chonghe"):
                values["kangmin"]["ios"]["scheme"] = scheme
                config.write_text(json.dumps(values))
                for pipeline, channel in (("test", "pgyer"), ("test", "testflight"), ("release", "")):
                    with self.subTest(scheme=scheme, pipeline=pipeline, channel=channel):
                        cmp = load_ios_brand_config(config, "kangmin", pipeline, channel)
                        kuikly = load_ios_brand_config(config, "kangmin", pipeline, channel, "kuikly")
                        expected_scheme = ("Kangmin" if scheme == "kangmin" else scheme) + "Kuikly"
                        self.assertEqual(cmp._replace(scheme=expected_scheme, ui_engine="kuikly"), kuikly)
            with self.assertRaisesRegex(DeliveryContextError, "UI 引擎"):
                load_ios_brand_config(config, "kangmin", "test", ui_engine="unknown")

    def test_ipa_engine_requires_renderer_inside_the_root_app(self):
        with tempfile.TemporaryDirectory() as temporary:
            for engine in ("cmp", "kuikly"):
                ipa = self._write_ipa(Path(temporary), ui_engine=engine)
                validate_ipa_ui_engine(ipa, engine)
                with self.assertRaisesRegex(IosReleaseError, "实际 UI 引擎"):
                    validate_ipa_ui_engine(ipa, "kuikly" if engine == "cmp" else "cmp")
            ipa = self._write_ipa(Path(temporary))
            with zipfile.ZipFile(ipa, "a") as archive:
                archive.writestr("Payload/Other.app/Frameworks/OpenKuiklyIOSRender.framework/OpenKuiklyIOSRender", b"wrong host")
                archive.writestr("Payload/Kangmin.app/Frameworks/OpenKuiklyIOSRender.framework/Info.plist", b"not executable")
            validate_ipa_ui_engine(ipa, "cmp")
            with self.assertRaisesRegex(IosReleaseError, "实际 UI 引擎"):
                validate_ipa_ui_engine(ipa, "kuikly")

    def test_kuikly_context_and_distribution_records_keep_engine(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for pipeline, channel in (("test", "pgyer"), ("test", "testflight"), ("release", "")):
                config, ipa, output = self._prepare(root, pipeline=pipeline, channel=channel, ui_engine="kuikly")
                context = json.loads((output / "context.json").read_text())
                self.assertEqual("kuikly", context["ui_engine"])
                self.assertEqual("KangminKuikly", context["scheme"])
                args = argparse.Namespace(config=config, context=output / "context.json",
                                          output=output / "release-info.json", dry_run=True)
                (upload_test_release if channel == "pgyer" else record_app_store_upload)(args)
                release = json.loads(args.output.read_text())
                self.assertEqual("kuikly", release["ui_engine"])
                if channel == "pgyer":
                    response = ArtifactInfo.from_dict(release)._replace(ui_engine="cmp")
                    with patch("delivery.ios_release.upload_artifact", return_value=response) as upload:
                        args.dry_run = False
                        upload_test_release(args)
                    upload.assert_called_once()
                    self.assertEqual("kuikly", json.loads(args.output.read_text())["ui_engine"])
                    args.dry_run = True
                notify_args = argparse.Namespace(config=config, context=args.context,
                                                 release_info=args.output, dry_run=True)
                notify_release(notify_args)
                release["ui_engine"] = "cmp"
                args.output.write_text(json.dumps(release))
                with self.assertRaisesRegex(IosReleaseError, "UI 引擎不一致"):
                    notify_release(notify_args)
                self._write_ipa(root)
                with self.assertRaisesRegex(IosReleaseError, "实际 UI 引擎"):
                    (upload_test_release if channel == "pgyer" else record_app_store_upload)(args)

    def test_prepare_rejects_wrong_host_before_writing_delivery_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ipa = self._write_ipa(root)
            with patch.object(self, "_write_ipa", return_value=ipa):
                with self.assertRaisesRegex(IosReleaseError, "实际 UI 引擎"):
                    self._prepare(root, ui_engine="kuikly")
            self.assertFalse((root / "output/context.json").exists())

    def test_test_branches_resolve_kangmin(self):
        for branch in (
            "feature/kangmin/ios-jenkins",
            "dev/kangmin",
            "release/kangmin-1.0.0",
        ):
            self.assertEqual("kangmin", resolve_target_brand("test", branch=branch))

    def test_classifies_formal_and_testflight_targets(self):
        cases = (
            (
                {"pipeline": "release", "tag": "kangmin-ios-v1.0.0"},
                ("1.0.0", "prod", "appstore", "app-store-connect"),
            ),
            (
                {
                    "pipeline": "test",
                    "branch": "feature/kangmin/ios-testflight",
                    "channel": "testflight",
                },
                (None, "dev", "appstore", "testflight"),
            ),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                target = classify_release_target("kangmin", **arguments)
                self.assertEqual(expected, tuple(target[1:]))

    def test_rejects_invalid_targets_in_brand_channel_reference_order(self):
        cases = (
            ("kangmin", "release", {"tag": "kangmin-v1.0.0"},
             "iOS 正式发布 Tag 必须使用 <brand>-ios-v<version>，当前为：kangmin-v1.0.0"),
            ("kangmin", "release", {"tag": "kangmin-ios-v1.0.0", "channel": "testflight"},
             "iOS 正式发布不接受测试分发渠道：testflight"),
            ("INVALID", "release", {"tag": "bad", "channel": "bad"},
             "品牌参数格式无效：INVALID"),
            ("kangmin", "release", {"tag": "bad", "channel": "bad"},
             "iOS 正式发布不接受测试分发渠道：bad"),
            ("kangmin", "test", {"branch": "master", "tag": "bad", "channel": "bad"},
             "iOS 测试分发渠道必须为 pgyer 或 testflight：bad"),
            ("kangmin", "test", {"branch": "master", "tag": " bad "},
             "iOS 测试包 Job 不接受 Tag：bad"),
            ("kangmin", "test", {"branch": "origin/dev/miaoshou"},
             "iOS 测试包分支品牌必须为 kangmin，当前分支为：dev/miaoshou"),
            ("kangmin", "release", {"tag": "miaoshou-ios-v1.0.0"},
             "iOS 正式发布 Tag 必须使用 kangmin-ios-v<version>，当前为：miaoshou-ios-v1.0.0"),
        )
        for brand, pipeline, arguments, message in cases:
            with self.subTest(brand=brand, pipeline=pipeline, arguments=arguments):
                with self.assertRaises(IosReleaseError) as error:
                    classify_release_target(brand, pipeline, **arguments)
                self.assertEqual(message, str(error.exception))

    def test_loads_only_explicit_ios_brand_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._write_config(Path(directory))

            ios = load_ios_brand_config(config, "kangmin", "test")
            self.assertEqual("kangmin", ios.scheme)
            self.assertEqual("TESTTEAM01", ios.team_id)
            self.assertEqual("dev", ios.environment)
            self.assertEqual("adhoc", ios.distribution)
            self.assertEqual(
                "ios-team-test-owner-distribution-p12",
                ios.distribution_p12_credential_id,
            )
            self.assertEqual("signing/ios/test_owner", ios.repository_signing_path)
            self.assertEqual("Test Owner AdHoc", ios.profile_name)
            self.assertEqual(
                "ios-team-test-owner-asc-api-key",
                ios.asc_api_key_credential_id,
            )
            testflight = load_ios_brand_config(
                config, "kangmin", "test", "testflight"
            )
            self.assertEqual("appstore", testflight.distribution)
            self.assertEqual("Test Owner AppStore", testflight.profile_name)
            with self.assertRaises(IosReleaseError):
                load_ios_brand_config(config, "miaoshou", "test")

    def test_test_and_release_can_use_different_developer_teams(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._write_config(Path(directory))

            test_config = load_ios_brand_config(config, "kangmin", "test")
            release_config = load_ios_brand_config(config, "kangmin", "release")

        self.assertEqual("TESTTEAM01", test_config.team_id)
        self.assertEqual("RELTEAM001", release_config.team_id)
        self.assertEqual(
            "ios-team-test-owner-distribution-p12",
            test_config.distribution_p12_credential_id,
        )
        self.assertEqual(
            "ios-team-test-owner-asc-api-key",
            test_config.asc_api_key_credential_id,
        )
        self.assertEqual(
            "ios-team-release-owner-asc-api-key",
            release_config.asc_api_key_credential_id,
        )
        self.assertEqual("Release Owner AppStore", release_config.profile_name)

    def test_disabled_release_requires_explicit_team_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._write_config(root)
            value = json.loads(config.read_text(encoding="utf-8"))
            value["kangmin"]["ios"]["signing"]["release"] = {
                "enabled": False,
                "blockReason": "待确认正式 Team",
            }
            config.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaisesRegex(IosReleaseError, "待确认正式 Team"):
                load_ios_brand_config(config, "kangmin", "release")

    def test_reads_and_validates_ipa_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            ipa = self._write_ipa(Path(directory))

            metadata = validate_ipa(ipa, "com.example.ios", "1.2.3")

        self.assertEqual("20260829153000", metadata.build_number)
        self.assertEqual("康民中医", metadata.display_name)

    def test_rejects_test_ipa_using_production_sdk_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._write_config(root)
            ipa = self._write_ipa(root)
            with zipfile.ZipFile(str(ipa), "r") as archive:
                entry = "Payload/Kangmin.app/Info.plist"
                info = plistlib.loads(archive.read(entry))
            info["SkillCureSdkProduction"] = "YES"
            with zipfile.ZipFile(str(ipa), "w") as archive:
                archive.writestr(entry, plistlib.dumps(info, fmt=plistlib.FMT_BINARY))

            with self.assertRaisesRegex(IosReleaseError, "SkillCureSdkProduction"):
                validate_ipa_build_settings(
                    ipa,
                    load_ios_brand_config(config, "kangmin", "test"),
                )

    def test_test_pipeline_prepares_and_dry_runs_distribution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, ipa, output = self._prepare(root, with_work_item=True)
            context = json.loads((output / "context.json").read_text(encoding="utf-8"))
            self.assertEqual("Release", context["build_type"])
            self.assertEqual("dev", context["environment"])
            self.assertEqual("adhoc", context["distribution"])
            self.assertEqual("pgyer", context["delivery"])
            self.assertEqual("com.example.ios", context["bundle_id"])
            self.assertEqual("ios", context["platform"])
            self.assertEqual(str(ipa.resolve()), context["artifact_path"])
            self.assertEqual("20260829153000", context["version_code"])
            self.assertEqual("bug", context["work_item"]["type"])
            self.assertEqual("2048", context["work_item"]["id"])

            common_args = argparse.Namespace(
                config=config,
                context=output / "context.json",
                output=output / "release-info.json",
                dry_run=True,
            )
            upload_test_release(common_args)
            notify_release(
                argparse.Namespace(
                    config=config,
                    context=output / "context.json",
                    release_info=output / "release-info.json",
                    dry_run=True,
                )
            )

    def test_testflight_pipeline_keeps_test_environment_and_records_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, _, output = self._prepare(
                root,
                channel="testflight",
            )
            context = json.loads((output / "context.json").read_text(encoding="utf-8"))
            self.assertEqual("dev", context["environment"])
            self.assertEqual("appstore", context["distribution"])
            self.assertEqual("testflight", context["delivery"])

            record_app_store_upload(
                argparse.Namespace(
                    config=config,
                    context=output / "context.json",
                    output=output / "release-info.json",
                    dry_run=True,
                )
            )
            release_info = json.loads(
                (output / "release-info.json").read_text(encoding="utf-8")
            )
            self.assertEqual("DRY-RUN", release_info["build_key"])

    def test_release_pipeline_dry_run_records_app_store_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, _, output = self._prepare(root, pipeline="release")
            record_app_store_upload(
                argparse.Namespace(
                    config=config,
                    context=output / "context.json",
                    output=output / "release-info.json",
                    dry_run=True,
                )
            )

            release_info = json.loads(
                (output / "release-info.json").read_text(encoding="utf-8")
            )
            self.assertEqual("DRY-RUN", release_info["build_key"])

    def test_rejects_ipa_bundle_id_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            ipa = Path(directory) / "Kangmin.ipa"
            with zipfile.ZipFile(str(ipa), "w") as archive:
                archive.writestr(
                    "Payload/Kangmin.app/Info.plist",
                    plistlib.dumps(
                        {
                            "CFBundleIdentifier": "com.wrong.ios",
                            "CFBundleShortVersionString": "1.0.0",
                            "CFBundleVersion": "100",
                        }
                    ),
                )

            with self.assertRaises(IosReleaseError):
                validate_ipa(ipa, "com.example.ios", "1.0.0")

    def test_rejects_invalid_ipa(self):
        with tempfile.TemporaryDirectory() as directory:
            ipa = Path(directory) / "invalid.ipa"
            ipa.write_bytes(b"not-a-zip")

            with self.assertRaises(IosReleaseError):
                read_ipa_metadata(ipa)


    def test_all_ipa_environments_reject_embedded_robot_credentials(self):
        for pipeline in ("test", "release"):
            for key in ("SkillCureDingTalkWebhook", "SkillCureDingTalkSecret"):
                with self.subTest(pipeline=pipeline, key=key), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    config = self._write_config(root)
                    ipa = self._write_ipa(root, pipeline=pipeline)
                    entry = "Payload/Kangmin.app/Info.plist"
                    with zipfile.ZipFile(ipa) as archive:
                        info = plistlib.loads(archive.read(entry))
                    info[key] = "test-only-placeholder"
                    with zipfile.ZipFile(ipa, "w") as archive:
                        archive.writestr(entry, plistlib.dumps(info))
                    with self.assertRaisesRegex(IosReleaseError, "SkillCureDingTalkCredentials"):
                        validate_ipa_build_settings(ipa, load_ios_brand_config(config, "kangmin", pipeline))


if __name__ == "__main__":
    unittest.main()
