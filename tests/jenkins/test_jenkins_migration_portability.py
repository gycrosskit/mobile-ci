import json
import os
import shutil
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
JENKINS_DIR = REPO_ROOT / "jenkins"
LOCAL_DIR = JENKINS_DIR / "local"
REMOTE_DIR = JENKINS_DIR / "remote"


class JenkinsMigrationPortabilityTest(unittest.TestCase):
    def test_health_monitor_scripts_have_valid_shell_syntax(self):
        scripts = (
            LOCAL_DIR / "bootstrap-mac-agent.sh",
            LOCAL_DIR / "run-health-monitor.sh",
            REMOTE_DIR / "install-health-monitor-systemd.sh",
            REMOTE_DIR / "run-health-monitor-systemd.sh",
            JENKINS_DIR / "verify-migration.sh",
        )
        for script in scripts:
            with self.subTest(script=script.name):
                self.assertTrue(os.access(script, os.X_OK))
                result = subprocess.run(
                    ["bash", "-n", str(script)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, result.returncode, result.stderr)

    def test_service_agent_help_resolves_migrated_version_files(self):
        result = subprocess.run(["bash", str(LOCAL_DIR / "install-pr-agent-launchdaemon.sh"), "--help"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Usage:", result.stdout)

    def test_mac_bootstrap_can_install_health_monitor(self):
        bootstrap = (LOCAL_DIR / "bootstrap-mac-agent.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("--install-health-monitor", bootstrap)
        self.assertIn("DINGTALK_WEBHOOK_FILE", bootstrap)
        self.assertIn("JENKINS_MONITOR_TOKEN_FILE", bootstrap)
        self.assertIn('install-health-monitor-launchd.sh', bootstrap)
        self.assertIn('TRUSTED_XCODES_TAP="xcodesorg/made"', bootstrap)
        self.assertIn('brew trust --tap "${TRUSTED_XCODES_TAP}"', bootstrap)
        self.assertIn('if [[ "${INSTALL_XCODE}" == "true" ]]', bootstrap)

    def test_legacy_mac_installer_delegates_to_service_account_with_secret_precedence(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            shutil.copy(LOCAL_DIR / "install-agent-launchd.sh", directory)
            (directory / "install-pr-agent-launchdaemon.sh").write_text(
                "python3 - \"$@\" <<'PY'\nimport json, os, sys\n"
                'print(json.dumps({"args":sys.argv[1:], "secret":os.environ["JENKINS_PR_AGENT_SECRET_FILE"]}))\nPY\n'
            )
            for preferred, expected in (("", "legacy-secret"), ("service-secret", "service-secret")):
                with self.subTest(preferred=preferred):
                    result = subprocess.run(
                        ["bash", str(directory / "install-agent-launchd.sh"), "--accept-android-licenses"],
                        env=dict(os.environ, JENKINS_AGENT_SECRET_FILE="legacy-secret", JENKINS_PR_AGENT_SECRET_FILE=preferred),
                        capture_output=True, text=True,
                    )
                    self.assertEqual(0, result.returncode, result.stderr)
                    invocation = json.loads(result.stdout)
                    self.assertEqual(["install", "--accept-android-licenses"], invocation["args"])
                    self.assertEqual(expected, invocation["secret"])

    def test_bootstrap_connects_one_service_agent_with_or_without_license_flag(self):
        bootstrap = (LOCAL_DIR / "bootstrap-mac-agent.sh").read_text()
        connect = bootstrap.split('if [[ "${CONNECT_AGENT}" == "true" ]]; then', 1)[1]
        connect = 'if [[ "${CONNECT_AGENT}" == "true" ]]; then' + connect.split('if [[ "${INSTALL_HEALTH_MONITOR}"', 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "install-pr-agent-launchdaemon.sh").write_text(
                'printf "%s\\n" "$@"\n'
            )
            for accepted, expected in (("false", ["install"]), ("true", ["install", "--accept-android-licenses"])):
                with self.subTest(accepted=accepted):
                    result = subprocess.run(
                        ["/bin/bash", "-eu", "-c", connect],
                        env=dict(os.environ, SCRIPT_DIR=str(directory), ACTION="install", CONNECT_AGENT="true", ACCEPT_ANDROID_LICENSES=accepted),
                        capture_output=True, text=True,
                    )
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual(expected, result.stdout.splitlines())

    def test_mac_service_bootstrap_retries_once_and_preserves_final_failure(self):
        installer = (LOCAL_DIR / "install-pr-agent-launchdaemon.sh").read_text()
        start = installer.index('  if ! /usr/bin/sudo /bin/launchctl bootstrap system')
        branch = installer[start:installer.index("\n}", start)]
        branch = branch.replace('/usr/bin/sudo /bin/launchctl', '"$FAKE_LAUNCHCTL"')
        branch = branch.replace('/bin/sleep', 'sleep')
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            launchctl = directory / "launchctl"
            calls = directory / "calls"
            sleeps = directory / "sleeps"
            launchctl.write_text(
                '#!/bin/bash\n'
                'printf "%s\\n" "$*" >> "$CALLS"\n'
                'if [[ "$(wc -l < "$CALLS")" -eq 1 ]]; then exit "$FIRST_STATUS"; fi\n'
                'exit "$SECOND_STATUS"\n'
            )
            launchctl.chmod(0o755)
            for first, second, expected, count in ((0, 5, 0, 1), (5, 0, 0, 2), (5, 23, 23, 2)):
                with self.subTest(first=first, second=second):
                    calls.write_text("")
                    sleeps.write_text("")
                    result = subprocess.run(
                        ["/bin/bash", "-eu", "-c", 'sleep() { printf "%s\\n" "$1" >> "$SLEEPS"; }\n' + branch + '\nprintf reached'],
                        env=dict(os.environ, FAKE_LAUNCHCTL=str(launchctl), CALLS=str(calls), SLEEPS=str(sleeps),
                                 PLIST_TARGET="service.plist", FIRST_STATUS=str(first), SECOND_STATUS=str(second)),
                        capture_output=True, text=True,
                    )
                    self.assertEqual(expected, result.returncode, result.stderr)
                    self.assertEqual(["bootstrap system service.plist"] * count, calls.read_text().splitlines())
                    self.assertEqual(["2"] if count == 2 else [], sleeps.read_text().splitlines())
                    self.assertEqual("reached" if expected == 0 else "", result.stdout)

    def test_mac_monitor_installs_importable_packages_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            tools = directory / "bin"
            tools.mkdir()
            # Only temporary files are installed; service control is an inert executable.
            for name in ("launchctl", "plutil"):
                tool = tools / name
                tool.write_text("#!/bin/sh\nexit 0\n")
                tool.chmod(0o755)
            secret = directory / "fixture-secret"
            secret.write_text("fixture-secret-value")
            install_root = directory / "installed monitor"
            result = subprocess.run(["bash", str(LOCAL_DIR / "install-health-monitor-launchd.sh")],
                                    env=dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"],
                                             HOME=str(directory / "home"),
                                             JENKINS_MONITOR_INSTALL_ROOT=str(install_root),
                                             DINGTALK_WEBHOOK_FILE=str(secret), DINGTALK_SECRET_FILE=str(secret)),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn(secret.read_text(), result.stdout + result.stderr)
            for relative in ("jenkins/__init__.py", "jenkins/jenkins_health_monitor.py",
                             "delivery/__init__.py", "delivery/dingtalk_notify.py", "delivery/delivery.py"):
                self.assertEqual(0o600, (install_root / relative).stat().st_mode & 0o777)
            for package in ("jenkins", "delivery"):
                self.assertEqual(0o700, (install_root / package).stat().st_mode & 0o777)
            smoke = subprocess.run([sys.executable, str(install_root / "jenkins/jenkins_health_monitor.py"), "--help"],
                                   env={"PATH": os.environ["PATH"], "PYTHONPATH": str(install_root)},
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(0, smoke.returncode, smoke.stderr)

    def test_mac_monitor_does_not_inherit_unrelated_tokens(self):
        runner = (LOCAL_DIR / "run-health-monitor.sh").read_text(encoding="utf-8")
        template = (
            LOCAL_DIR / "com.sxmqlive.jenkins-health-monitor.plist.template"
        ).read_text(encoding="utf-8")

        self.assertIn('/usr/bin/env -i "${MONITOR_ENV[@]}"', runner)
        self.assertIn("GITEE_ACCESS_TOKEN", template)
        self.assertIn("<string></string>", template)
        self.assertNotIn("export DINGTALK_WEBHOOK", runner)

    def test_linux_monitor_uses_root_only_credentials_and_sandbox(self):
        installer = (REMOTE_DIR / "install-health-monitor-systemd.sh").read_text(
            encoding="utf-8"
        )
        runner = (REMOTE_DIR / "run-health-monitor-systemd.sh").read_text(
            encoding="utf-8"
        )
        service = (
            REMOTE_DIR
            / "systemd"
            / "sxmqlive-jenkins-health-monitor.service.template"
        ).read_text(encoding="utf-8")
        timer = (
            REMOTE_DIR / "systemd" / "sxmqlive-jenkins-health-monitor.timer"
        ).read_text(encoding="utf-8")

        for variable in (
            "DINGTALK_WEBHOOK_FILE",
            "DINGTALK_SECRET_FILE",
            "JENKINS_MONITOR_USER_FILE",
            "JENKINS_MONITOR_TOKEN_FILE",
        ):
            self.assertIn(variable, installer)
        self.assertIn("systemd-analyze verify", installer)
        self.assertIn("enable --now sxmqlive-jenkins-health-monitor.timer", installer)
        self.assertIn('STATE_ROOT="/var/lib/sxmqlive-jenkins-monitor"', installer)
        self.assertNotIn("--value", installer)
        self.assertIn("CREDENTIALS_DIRECTORY", runner)
        self.assertIn('/usr/bin/env -i "${MONITOR_ENV[@]}"', runner)
        self.assertIn("PYTHONIOENCODING=UTF-8", runner)
        self.assertIn("PYTHONUTF8=1", runner)
        self.assertIn("User=root", service)
        self.assertIn(
            "Environment=CREDENTIALS_DIRECTORY=/etc/sxmqlive-jenkins-monitor",
            service,
        )
        self.assertIn(
            "Environment=STATE_DIRECTORY=/var/lib/sxmqlive-jenkins-monitor",
            service,
        )
        self.assertNotIn("LoadCredential=", service)
        self.assertIn("NoNewPrivileges=yes", service)
        self.assertIn("ProtectSystem=full", service)
        self.assertIn("OnUnitActiveSec=5min", timer)
        self.assertIn("Persistent=true", timer)

    def test_migration_verifier_covers_both_supported_hosts(self):
        verifier = (JENKINS_DIR / "verify-migration.sh").read_text(encoding="utf-8")

        self.assertIn("mac-agents", verifier)
        self.assertIn("remote-linux", verifier)
        self.assertNotIn("local-mac", verifier)
        self.assertIn("install-pr-agent-launchdaemon.sh", verifier)
        self.assertIn("system/com.sxmqlive.jenkins-pr-agent", verifier)
        self.assertIn("Legacy personal Mac Agent is still registered", verifier)
        self.assertIn("com.sxmqlive.jenkins-controller-tunnel", verifier)
        self.assertNotIn("controller.sh", verifier)
        self.assertNotIn("backup.sh", verifier)
        self.assertNotIn("restore.sh", verifier)
        self.assertIn('stack.sh" verify', verifier)
        self.assertIn("sync_jenkins_jobs.py", verifier)
        self.assertIn("sync_jenkins_admin_jobs.py", verifier)
        self.assertIn("health-monitor state is stale", verifier)
        self.assertIn("export PYTHONIOENCODING=UTF-8", verifier)
        self.assertIn("export PYTHONUTF8=1", verifier)
        self.assertIn("LC_ALL=en_US.utf8 verify_job_configuration", verifier)


if __name__ == "__main__":
    unittest.main()
