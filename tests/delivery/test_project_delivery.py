import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import project
import project_config
from delivery import delivery, pgyer_upload, ios_release, harmony_release, dingtalk_notify

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/delivery/fixtures/projects/example-mobile.json"


class ProjectDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.profiles = self.root / "profiles"
        self.profiles.mkdir()
        shutil.copy(FIXTURE, self.profiles / FIXTURE.name)
        shutil.copy(ROOT / "projects/sxmqlive.json", self.profiles / "sxmqlive.json")
        for context in (patch.object(project_config, "PROJECTS_DIR", self.profiles),
                        patch.dict(os.environ, {"CI_PROJECT_ID":"example-mobile", "JOB_NAME":"", "DELIVERY_STATE_ROOT":str(self.root / "state")})):
            context.start(); self.addCleanup(context.stop)
        self.apk = self.root / "fixture.apk"
        self.apk.write_bytes(b"mock signed APK")
        self.artifact = delivery.ArtifactInfo("Example", "1.0.0", "1", "org.example.mobile", "key", "https://www.pgyer.com/abc", "")

    def upload(self, key="first-key"):
        engine = "cmp" if os.environ["CI_PROJECT_ID"] == "sxmqlive" else "native"
        return pgyer_upload.upload_artifact(str(self.apk), key, "fixture", "android", delivery_identity={"brand":"example", "ui_engine":engine})

    def change_account(self):
        path = self.profiles / FIXTURE.name
        profile = json.loads(path.read_text())
        profile["providers"]["pgyer"].update(accountId="example-other", credentialId="example-other-key")
        path.write_text(json.dumps(profile))

    def test_logical_account_rotation_reuses_receipt_and_new_account_uploads(self):
        with patch.object(pgyer_upload, "get_cos_token", return_value=("https://fixture.test", "key", {})) as token, patch.object(pgyer_upload, "upload_file") as upload, patch.object(pgyer_upload, "wait_for_release", return_value=self.artifact):
            self.assertEqual(self.artifact._replace(ui_engine="native"), self.upload())
            self.assertEqual(self.artifact._replace(ui_engine="native"), self.upload("rotated-key"))
            self.assertEqual(1, token.call_count)
            self.change_account()
            self.assertEqual(self.artifact._replace(ui_engine="native"), self.upload("other-account-key"))
            self.assertEqual(2, token.call_count); self.assertEqual(2, upload.call_count)
        raw = "".join(p.read_text() for p in (self.root / "state").glob("*.json"))
        self.assertNotIn("first-key", raw); self.assertNotIn("rotated-key", raw)

    def test_pending_account_switch_and_unknown_legacy_namespace_cannot_reupload(self):
        with patch.object(pgyer_upload, "get_cos_token", side_effect=pgyer_upload.PgyerError("unknown")) as token:
            with self.assertRaises(pgyer_upload.PgyerError): self.upload()
            self.change_account()
            with self.assertRaisesRegex(delivery.DeliveryContextError, "旧供应商账号结果未知"): self.upload()
            self.assertEqual(1, token.call_count)
        shutil.rmtree(self.root / "state")
        scope = {"operation":"pgyer-upload", "platform":"android", "brand":"example"}
        with delivery.delivery_operation(scope, scope=scope) as (_, save): save(phase="pending")
        with patch.object(pgyer_upload, "get_cos_token") as token:
            with self.assertRaisesRegex(delivery.DeliveryContextError, "旧版交付回执"): self.upload()
            token.assert_not_called()

    def test_distinct_projects_do_not_share_receipts_or_locks(self):
        with patch.object(pgyer_upload, "get_cos_token", return_value=("https://fixture.test", "key", {})) as token, patch.object(pgyer_upload, "upload_file"), patch.object(pgyer_upload, "wait_for_release", return_value=self.artifact):
            with patch.dict(os.environ, {"CI_PROJECT_ID":"sxmqlive"}): self.upload()
            self.upload()
            self.assertEqual(2, token.call_count)
        states = list((self.root / "state").glob("*.json"))
        self.assertEqual(2, len(states))
        self.assertEqual({"sxmqlive", "example-mobile"}, {json.loads(p.read_text())["namespace"]["project_id"] for p in states})

    def test_legacy_default_completed_and_pending_stay_on_original_key(self):
        profile = json.loads((self.profiles / "sxmqlive.json").read_text())
        profile["providers"]["pgyer"].update(legacyNamespace=True, accountId="sxmqlive-pgyer")
        (self.profiles / "sxmqlive.json").write_text(json.dumps(profile))
        scope = {"operation":"pgyer-upload", "platform":"android", "brand":"example"}
        identity = {**scope, "artifact_sha256":delivery.artifact_sha256(self.apk), "channel_shortcut":"", "ui_engine":"cmp"}
        with delivery.delivery_operation(identity, scope=scope) as (_, save):
            save(phase="complete", build_key="key", receipt=self.artifact.to_dict())
        before = list((self.root / "state").glob("*.json"))
        with patch.dict(os.environ, {"CI_PROJECT_ID":"sxmqlive"}), patch.object(pgyer_upload, "get_cos_token") as token:
            self.assertEqual(self.artifact, self.upload()); token.assert_not_called()
        self.assertEqual(before, list((self.root / "state").glob("*.json")))
        value = json.loads(before[0].read_text()); value["phase"] = "pending"; before[0].write_text(json.dumps(value))
        with patch.dict(os.environ, {"CI_PROJECT_ID":"sxmqlive"}), patch.object(pgyer_upload, "get_cos_token") as token:
            with self.assertRaisesRegex(pgyer_upload.PgyerError, "未知"): self.upload()
            token.assert_not_called()

    def test_job_binding_and_path_escape_fail_closed(self):
        with patch.dict(os.environ, {"JOB_NAME":"mobile-apps/sxmqlive-android-test"}):
            with self.assertRaises(delivery.DeliveryContextError): delivery.delivery_namespace("pgyer")
        with patch.object(project, "SOURCE_ROOT", self.root):
            with self.assertRaises(project_config.ProjectConfigError): project.app_path("../outside")
            (self.root / "linked").symlink_to(self.root.parent)
            with self.assertRaises(project_config.ProjectConfigError): project.app_path("linked/file")

    def test_real_native_git_project_cli_shell_and_artifact_identity(self):
        app = self.root / "application"; app.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(app)], check=True, capture_output=True)
        (app / "gradlew").write_text('printf "%s\\n" "$@" > "$CAPTURE"\n')
        config = app / "ci/brands.json"; config.parent.mkdir(); config.write_text(json.dumps({"example":{"displayName":"Example", "testers":[]}}))
        subprocess.run(["git", "-C", str(app), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(app), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "fixture"], check=True, capture_output=True)
        apk = app / "mobile/build/outputs/apk/debug/mobile-debug.apk"; apk.parent.mkdir(parents=True)
        with zipfile.ZipFile(apk, "w") as archive: archive.writestr("AndroidManifest.xml", "mock")
        metadata = {"variantName":"debug", "applicationId":"org.example.mobile", "elements":[{"outputFile":apk.name,"filters":[],"versionName":"1.0.0","versionCode":1}]}
        (apk.parent / "output-metadata.json").write_text(json.dumps(metadata))
        tools = self.root / "trusted-ci"; tools.mkdir(); shutil.copy(ROOT / "project.py", tools); shutil.copy(ROOT / "project_config.py", tools)
        shutil.copytree(ROOT / "delivery", tools / "delivery"); shutil.copytree(self.profiles, tools / "projects")
        environment = {**os.environ, "CI_TOOL_ROOT":str(tools), "APP_SOURCE_ROOT":str(app), "CAPTURE":str(self.root / "capture"), "CI_UI_ENGINE":"native"}
        result = subprocess.run(["bash", str(tools / "delivery/android_ci.sh"), "verified-build", "example", "test", "native"], env=environment, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        captured = (self.root / "capture").read_text(); self.assertIn(":mobile:assembleDebug", captured); self.assertNotIn("cmpBrand", captured)
        command = [sys.executable, str(tools / "delivery/release.py"), "prepare", "--repo", str(app), "--config", str(config), "--brand", "example", "--pipeline", "test", "--ui-engine", "native", "--manual-changelog", "fixture", "--output-dir", str(app / "out")]
        resolve = [sys.executable, str(tools / "delivery/release.py"), "resolve-brand", "--pipeline", "test", "--branch", "main", "--brand", "example"]
        result = subprocess.run(resolve, env=environment, capture_output=True, text=True)
        self.assertEqual((0, "example"), (result.returncode, result.stdout.strip()), result.stderr)
        result = subprocess.run(resolve[:-1] + ["unregistered"], env=environment, capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode)
        result = subprocess.run(command, env=environment, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        context = json.loads((app / "out/context.json").read_text()); self.assertEqual("main", context["branch"]); self.assertEqual("native", context["ui_engine"]); self.assertRegex(context["commit"], r"^[0-9a-f]{40}$")
        metadata["applicationId"] = "org.wrong"; (apk.parent / "output-metadata.json").write_text(json.dumps(metadata))
        result = subprocess.run(command, env=environment, capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode); self.assertIn("applicationId", result.stderr)

    def test_native_ipa_requires_bundle_identity_without_inferred_cmp(self):
        ipa = self.root / "native.ipa"
        with zipfile.ZipFile(ipa, "w") as archive:
            archive.writestr("Payload/Native.app/Info.plist", plistlib.dumps({"CFBundleIdentifier":"org.example.mobile.ios","CFBundleShortVersionString":"1.0.0","CFBundleVersion":"1"}))
        self.assertEqual("org.example.mobile.ios", ios_release.validate_ipa(ipa, "org.example.mobile.ios", "1.0.0").bundle_id)
        ios_release.validate_ipa_ui_engine(ipa, "native")
        with self.assertRaises(ios_release.IosReleaseError): ios_release.validate_ipa(ipa, "org.wrong", None)


    def test_native_xcode_shell_and_cli_use_declared_project_and_identity(self):
        app = self.root / "xcode-app"; app.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(app)], check=True, capture_output=True)
        (app / "Native.xcodeproj").mkdir(); (app / "Configuration").mkdir()
        (app / "Configuration/Test.xcconfig").write_text("PRODUCT_BUNDLE_IDENTIFIER=org.example.mobile.ios\n")
        config = app / "brands.json"
        config.write_text(json.dumps({"example":{"displayName":"Example", "testers":[], "ios":{"scheme":"Native","bundleId":"org.example.mobile.ios", "build":{"test":{"environment":"dev","distribution":"adhoc","xcconfig":"Configuration/Test.xcconfig"}}, "signing":{"test":{"enabled":True,"team":"example","profileNames":{"pgyer":"Example Distribution"}}}}}, "_iosTeams":{"example":{"teamId":"ABCDE12345","credentialNamespace":"ios-team-example","repositorySigningPath":"signing/ios/example"}}}))
        subprocess.run(["git", "-C", str(app), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(app), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "fixture"], check=True, capture_output=True)
        tools = self.root / "xcode-ci"; tools.mkdir(); shutil.copy(ROOT / "project.py", tools); shutil.copy(ROOT / "project_config.py", tools)
        shutil.copytree(ROOT / "delivery", tools / "delivery"); shutil.copytree(self.profiles, tools / "projects")
        bin_dir = self.root / "bin"; bin_dir.mkdir()
        (bin_dir / "xcodebuild").write_text('#!/bin/bash\nprintf "%s\n" "$@"\nprintf "GRADLE=%s\n" "${GRADLE_OPTS-}"\n')
        (bin_dir / "xcodebuild").chmod(0o755)
        environment = {**os.environ, "PATH":str(bin_dir) + os.pathsep + os.environ["PATH"], "APP_SOURCE_ROOT":str(app), "CI_TOOL_ROOT":str(tools), "CI_UI_ENGINE":"native"}
        result = subprocess.run(["bash", str(tools / "delivery/ios_ci.sh"), "build-unsigned", "dev", "Native", "Configuration/Test.xcconfig"], env=environment, text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr); self.assertIn("-project\n" + str(app / "Native.xcodeproj"), result.stdout); self.assertIn("GRADLE=\n", result.stdout); self.assertNotIn("iosApp", result.stdout)
        ipa = app / "Native.ipa"
        with zipfile.ZipFile(ipa, "w") as archive:
            archive.writestr("Payload/Native.app/Info.plist", plistlib.dumps({"CFBundleIdentifier":"org.example.mobile.ios","CFBundleShortVersionString":"1.0.0","CFBundleVersion":"1"}))
        command = [sys.executable, str(tools / "delivery/ios_release.py"), "prepare", "--repo", str(app), "--config", str(config), "--brand", "example", "--pipeline", "test", "--channel", "pgyer", "--ui-engine", "native", "--ipa", str(ipa), "--manual-changelog", "fixture", "--output-dir", str(app / "out")]
        result = subprocess.run([sys.executable, str(tools / "delivery/ios_release.py"), "resolve-brand", "--config", str(config), "--pipeline", "test", "--channel", "pgyer", "--branch", "main", "--brand", "example"], env=environment, capture_output=True, text=True)
        self.assertEqual((0, "example"), (result.returncode, result.stdout.strip()), result.stderr)
        result = subprocess.run(command, env=environment, text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        context = json.loads((app / "out/context.json").read_text()); self.assertEqual(("native", "main", "org.example.mobile.ios"), (context["ui_engine"], context["branch"], context["bundle_id"]))

    def test_native_hvigor_build_uses_declared_module_product_and_standard_package(self):
        path = self.profiles / FIXTURE.name; profile = json.loads(path.read_text())
        profile["platforms"]["harmony"] = {"adapter":"hvigor", "engines":["native"], "module":"harmony/client", "product":"default", "artifact":"{module}/build/{product}/outputs/{product}/client-{product}-unsigned.hap", "appIdentifier":"org.example.mobile.harmony"}
        for role in ("harmony_test", "harmony_release"):
            profile["jobs"][role] = {"name":"example/" + role.replace("_", "-"), "pipeline":"jenkins/Jenkinsfile.ohos.test"}
        path.write_text(json.dumps(profile))
        app = self.root / "hvigor-app"; app.mkdir(); (app / "harmony/hvigor").mkdir(parents=True)
        (app / "harmony/build-profile.json5").write_text(json.dumps({"app":{"signingConfigs":[], "products":[{"name":"default"}]}}))
        (app / "harmony/hvigor/hvigor-config.json5").write_text("{}")
        config = {"brand":"example", "bundleName":"org.example.mobile.harmony", **harmony_release.build_settings("Release"), "uiEngine":"native"}
        output = app / "harmony/client/build/default/outputs/default/client-default-unsigned.hap"
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if "-e" in args: return "{}"
            if "assembleHap" in args:
                output.parent.mkdir(parents=True)
                with zipfile.ZipFile(output, "w") as archive:
                    archive.writestr("module.json", json.dumps({"app":{"bundleName":config["bundleName"], "versionName":"1.0.0", "versionCode":1, "debug":False}, "module":{"name":"client", "type":"entry"}}))
            return ""
        with patch.object(project, "SOURCE_ROOT", app), patch.object(harmony_release, "preflight", return_value={"sdk":app / "sdk", "node":app / "node/bin/node", "ohpm":app / "node/bin/ohpm", "hvigor":app / "hvigor/bin/hvigor.js"}), patch.object(harmony_release, "run", side_effect=run):
            self.assertEqual(output, harmony_release.build(app, config))
            self.assertEqual(config["bundleName"], harmony_release.hap_metadata(output, config)["bundleName"])
            with self.assertRaises(harmony_release.HarmonyReleaseError): harmony_release.hap_metadata(output, {**config, "bundleName":"org.wrong"})
        joined = " ".join(" ".join(args) for args in calls)
        self.assertIn("module=client@default", joined); self.assertIn("product=default", joined)
        self.assertNotIn("install_release_hars", joined); self.assertNotIn("assets-contract", joined)

    def test_notification_account_pending_cannot_be_bypassed_by_new_webhook(self):
        namespace, legacy = delivery.delivery_namespace("dingtalk")
        old = {"operation":"release-notify","platform":"Android","distribution":"pgyer","channel_sha256":"a" * 64,"build_key":"key"}
        with delivery.delivery_operation(old, scope=old, namespace=namespace, legacy_compatible=legacy) as (_, save): save(status="pending")
        path = self.profiles / FIXTURE.name; profile = json.loads(path.read_text()); profile["providers"]["dingtalk"].update(accountId="example-other", webhookCredentialId="example-other-hook"); path.write_text(json.dumps(profile))
        new = {**old, "channel_sha256":"b" * 64}
        namespace, legacy = delivery.delivery_namespace("dingtalk")
        with self.assertRaisesRegex(delivery.DeliveryContextError, "旧供应商账号结果未知"):
            with delivery.delivery_operation(new, scope=new, namespace=namespace, legacy_compatible=legacy): pass

    def test_native_notification_wrapper_uses_logical_account_across_token_rotation(self):
        brand = dingtalk_notify.BrandConfig("example", "Example", [], {})
        def notify(token):
            return dingtalk_notify.send_release_notification(self.artifact._replace(ui_engine="native"), brand, "fixture", "test", "main", "a" * 40, "fixture", "https://fixture.test?token=" + token, "secret", ui_engine="native", artifact_sha256="a" * 64)
        with patch.object(dingtalk_notify, "_send_with_retry") as send:
            notify("first"); notify("rotated"); self.assertEqual(1, send.call_count)
        shutil.rmtree(self.root / "state")
        with patch.object(dingtalk_notify, "_send_with_retry", side_effect=dingtalk_notify.DingTalkError("unknown")) as send:
            with self.assertRaises(dingtalk_notify.DingTalkError): notify("first")
            with self.assertRaisesRegex(dingtalk_notify.DingTalkError, "结果未知"): notify("rotated")
            self.assertEqual(1, send.call_count)

    def test_opaque_legacy_full_identity_or_scoped_writer_blocks_before_first_save(self):
        profile = json.loads((self.profiles / "sxmqlive.json").read_text())
        profile["providers"]["pgyer"].update(legacyNamespace=True, accountId="sxmqlive-pgyer")
        (self.profiles / "sxmqlive.json").write_text(json.dumps(profile))
        scope = {"operation":"pgyer-upload", "platform":"android", "brand":"example"}
        identity = {**scope, "artifact_sha256":delivery.artifact_sha256(self.apk), "channel_shortcut":"", "ui_engine":"native"}
        for old_scope in (None, scope):
            started = threading.Event(); release_writer = threading.Event(); completed = threading.Event(); errors = []
            def old_writer():
                old_identity = {**identity, "artifact_sha256":"b" * 64, "commit":"b" * 40} if old_scope is None else identity
                with delivery.delivery_operation(old_identity, scope=old_scope) as (_, save):
                    started.set(); release_writer.wait(3); save(phase="pending")
            def new_writer():
                try: self.upload()
                except Exception as error: errors.append(error)
                finally: completed.set()
            with patch.object(pgyer_upload, "get_cos_token") as token:
                old = threading.Thread(target=old_writer); old.start(); self.assertTrue(started.wait(2))
                new = threading.Thread(target=new_writer); new.start()
                try: self.assertTrue(completed.wait(1))
                finally: release_writer.set(); old.join(3); new.join(3)
                self.assertTrue(completed.is_set()); self.assertEqual(1, len(errors)); self.assertIsInstance(errors[0], delivery.DeliveryContextError)
                self.assertIn("旧版交付锁仍活跃", str(errors[0]))
                token.assert_not_called()
            shutil.rmtree(self.root / "state")


if __name__ == "__main__":
    unittest.main()
