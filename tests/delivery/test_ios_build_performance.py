import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from itertools import product
from pathlib import Path
from project import SOURCE_ROOT, TOOL_ROOT

ROOT = SOURCE_ROOT

class IosBuildPerformanceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        (root / "gradlew").touch()
        for host in ("iosApp", "iosKuiklyApp"):
            (root / host / (host + ".xcworkspace")).mkdir(parents=True)
        config = root / "iosApp/Configuration/Miaoshou.DevRelease.xcconfig"
        config.parent.mkdir(); config.touch()
        for context in (patch(__name__ + ".ROOT", root),
                        patch.dict(os.environ, APP_SOURCE_ROOT=str(root), CI_TOOL_ROOT=str(TOOL_ROOT))):
            context.start(); self.addCleanup(context.stop)

    def test_ios_preflight_rejects_sdk_with_ineligible_device_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sdk = directory / "iPhoneOS.sdk"
            sdk.mkdir()
            xcodebuild = directory / "xcodebuild"
            xcodebuild.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  -showsdks) echo 'iOS 26.2 -sdk iphoneos26.2' ;;\n"
                "  -showdestinations) printf '%s\\n' \"$@\" > \"$TEST_ARGUMENTS\"; printf '%s\\n' \"$TEST_DESTINATIONS\" ;;\n"
                "  -version) printf 'Xcode 26.3\\nBuild version 17C529\\n' ;;\n"
                "esac\n"
            )
            xcodebuild.chmod(0o755)
            xcrun = directory / "xcrun"
            xcrun.write_text(
                "#!/bin/sh\n"
                "if [ \"$3\" = '--show-sdk-path' ]; then\n"
                "  printf '%s\\n' \"$TEST_SDK\"\n"
                "else\n"
                "  echo '26.2'\n"
                "fi\n"
            )
            xcrun.chmod(0o755)
            pod = directory / "pod"
            pod.write_text("#!/bin/sh\necho '1.17.0'\n")
            pod.chmod(0o755)
            for name in ("security", "codesign", "ditto"):
                executable = directory / name
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)
            uname = directory / "uname"
            uname.write_text("#!/bin/sh\necho 'Darwin'\n")
            uname.chmod(0o755)
            base_env = dict(
                os.environ,
                PATH=str(directory) + os.pathsep + os.environ["PATH"],
                TEST_SDK=str(sdk),
                TEST_ARGUMENTS=str(directory / "arguments"),
            )
            for available, engine in product((False, True), ("cmp", "kuikly")):
                with self.subTest(available=available, engine=engine):
                    scheme = "ChongheKuikly" if engine == "kuikly" else "Chonghe"
                    host = "iosKuiklyApp" if engine == "kuikly" else "iosApp"
                    section = "Available" if available else "Ineligible"
                    destinations = (
                        f'{section} destinations for the "Chonghe" scheme:\n'
                        '    { platform:iOS, id:dvtdevice-DVTiPhonePlaceholder-iphoneos:placeholder, '
                        'name:Any iOS Device, error:iOS 26.2 is not installed. }'
                    )
                    result = subprocess.run(
                        ["bash", str(TOOL_ROOT / "delivery/ios_ci.sh"), "preflight", scheme],
                        env=dict(base_env, TEST_DESTINATIONS=destinations, CI_UI_ENGINE=engine),
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(0 if available else 1, result.returncode)
                    invocation = (directory / "arguments").read_text().splitlines()
                    self.assertEqual(scheme, invocation[invocation.index("-scheme") + 1])
                    self.assertEqual(str(ROOT / host / (host + ".xcodeproj")), invocation[invocation.index("-project") + 1])
                    if available:
                        default_result = subprocess.run(
                            ["bash", str(TOOL_ROOT / "delivery/ios_ci.sh"), "preflight"],
                            env=dict(base_env, TEST_DESTINATIONS=destinations, CI_UI_ENGINE=engine),
                            capture_output=True, text=True,
                        )
                        self.assertEqual(0, default_result.returncode, default_result.stderr)
                        default_invocation = (directory / "arguments").read_text().splitlines()
                        self.assertEqual("MiaoshouKuikly" if engine == "kuikly" else "Miaoshou",
                                         default_invocation[default_invocation.index("-scheme") + 1])
                    self.assertNotIn("dvtdevice-DVTiPhonePlaceholder", result.stdout + result.stderr)
                    if not available:
                        self.assertIn("Generic iOS device destination is unavailable", result.stderr)

    def test_xcode_timing_keeps_release_signing_and_failure_contracts(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            xcodebuild = directory / "xcodebuild"
            xcodebuild.write_text(
                '#!/bin/sh\n'
                'printf "GRADLE_OPTS=%s\\n" "${GRADLE_OPTS:-}"\n'
                'printf "CI_GRADLE_PROFILE=%s\\n" "${CI_GRADLE_PROFILE:-}"\n'
                'printf "%s\\n" "$@"\n'
                'exit "${XCODE_STATUS:-0}"\n'
            )
            xcodebuild.chmod(0o755)
            keychain = directory / "keychain"
            keychain.touch()
            for action, engine in product(("build-unsigned", "archive"), ("cmp", "kuikly")):
                for status in (0, 65):
                    with self.subTest(action=action, exit_status=status, engine=engine):
                        arguments = [action, "dev"]
                        if action == "archive":
                            arguments.append("adhoc")
                        scheme = "MiaoshouKuikly" if engine == "kuikly" else "Miaoshou"
                        arguments += [scheme, "iosApp/Configuration/Miaoshou.DevRelease.xcconfig"]
                        if action == "archive":
                            arguments += ["TESTTEAM", "123", "test profile", str(keychain)]
                        result = subprocess.run(
                            ["bash", str(TOOL_ROOT / "delivery/ios_ci.sh"), *arguments],
                            env=dict(
                                os.environ,
                                PATH=str(directory) + os.pathsep + os.environ["PATH"],
                                CI_OUTPUT_DIR=str(directory / f"output-{action}-{status}-{engine}"),
                                CI_UI_ENGINE=engine,
                                XCODE_STATUS=str(status),
                            ),
                            capture_output=True,
                            text=True,
                        )
                        self.assertEqual(status, result.returncode, result.stderr)
                        invocation = result.stdout.splitlines()
                        self.assertIn(
                            "GRADLE_OPTS=-Dorg.gradle.jvmargs=-Xmx6144m "
                            "-Dorg.gradle.workers.max=1 -Dorg.gradle.parallel=false",
                            invocation,
                        )
                        self.assertEqual(1, invocation.count("-showBuildTimingSummary"))
                        self.assertEqual("Release", invocation[invocation.index("-configuration") + 1])
                        host = "iosKuiklyApp" if engine == "kuikly" else "iosApp"
                        self.assertEqual(str(ROOT / host / (host + ".xcworkspace")), invocation[invocation.index("-workspace") + 1])
                        self.assertEqual(scheme, invocation[invocation.index("-scheme") + 1])
                        self.assertEqual(str(ROOT / "iosApp/Configuration/Miaoshou.DevRelease.xcconfig"), invocation[invocation.index("-xcconfig") + 1])
                        self.assertIn("generic/platform=iOS", invocation)
                        self.assertNotIn("clean", invocation)
                        if action == "build-unsigned":
                            self.assertIn("CI_GRADLE_PROFILE=", invocation)
                            self.assertIn("CODE_SIGNING_ALLOWED=NO", invocation)
                        else:
                            self.assertIn("CI_GRADLE_PROFILE=1", invocation)
                            self.assertIn("-onlyUsePackageVersionsFromResolvedFile", invocation)
                            self.assertIn("CODE_SIGN_STYLE=Manual", invocation)
                            self.assertIn("PROVISIONING_PROFILE_SPECIFIER=test profile", invocation)
                        self.assertFalse(
                            {"SKILLCURE_DEBUG_TOOLS_ENABLED", "SKILLCURE_DEFAULT_API_ENVIRONMENT"}
                            & set(invocation)
                        )

    def test_ios_shell_rejects_unknown_engine_before_invoking_tools(self):
        result = subprocess.run(
            ["bash", str(TOOL_ROOT / "delivery/ios_ci.sh"), "preflight"],
            env=dict(os.environ, CI_UI_ENGINE="unknown"), capture_output=True, text=True,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("UI 引擎", result.stderr)
