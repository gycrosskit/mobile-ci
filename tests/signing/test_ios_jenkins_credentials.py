import base64
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import shutil
from datetime import datetime, timedelta, timezone
import threading
import unittest
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from signing import ios_jenkins_credentials as credentials


class FakeController:
    def __init__(self):
        self.values = {}
        self.requests = []
        self.creates = []
        self.running = False
        self.queued = False
        self.race_busy = False
        self.race_conflict = False
        self.fail_apply = False
        self.fail_status = None
        self.raw_error = "private-password-private-token"
        self.redirect = None
        self.malformed = False

    def script(self, payload):
        if self.race_conflict and payload["apply"]:
            self.values[payload["items"][0]["id"]] = ("Secret file", b"other private material")
        rows = []
        for item in payload["items"]:
            existing = self.values.get(item["id"])
            status = "missing"
            if existing:
                kind, value = existing
                if kind != item["type"]:
                    status = "wrong-type"
                elif "sha256" not in item:
                    status = "present-unverified"
                else:
                    status = "matching" if hashlib.sha256(value).hexdigest() == item["sha256"] else "conflict"
            rows.append({"id": item["id"], "type": item["type"], "status": status})
        if payload["apply"] and not any(row["status"] in ("wrong-type", "conflict", "present-unverified") for row in rows):
            if self.race_busy:
                return {"error": "busy"}
            for row, item in zip(rows, payload["items"]):
                if row["status"] == "missing":
                    self.values[item["id"]] = (item["type"], base64.b64decode(item["value"]))
                    self.creates.append(item["id"])
                    row["status"] = "created"
        return {"credentials": rows}

    def handler(self):
        controller = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                controller.requests.append((self.command, self.path, body))
                if controller.redirect:
                    self.send_response(302)
                    self.send_header("Location", controller.redirect)
                    self.end_headers()
                    return
                if controller.fail_status:
                    self.send_response(controller.fail_status)
                    self.end_headers()
                    self.wfile.write(controller.raw_error.encode())
                    return
                if self.path.startswith("/queue/"):
                    result = json.dumps({"items": [{"id": 1}] if controller.queued else []})
                elif self.path.startswith("/computer/"):
                    result = json.dumps({"computer": [{"executors": [{"currentExecutable": {"url": "build"} if controller.running else None}], "oneOffExecutors": []}]})
                else:
                    script = urllib.parse.parse_qs(body.decode())["script"][0]
                    encoded = re.search(r"parseText\(new String\('([A-Za-z0-9+/=]+)'", script).group(1)
                    payload = json.loads(base64.b64decode(encoded))
                    response = controller.script(payload)
                    if controller.fail_apply and payload["apply"]:
                        self.send_response(500)
                        self.end_headers()
                        self.wfile.write(controller.raw_error.encode())
                        return
                    result = controller.raw_error if controller.malformed else credentials.MARKER + json.dumps(response) + "\nResult: null\n"
                self.send_response(200)
                self.end_headers()
                self.wfile.write(result.encode())
        return Handler


class IosJenkinsCredentialsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("openssl"):
            raise unittest.SkipTest("P12/P-256 行为测试需要 openssl")
        cls.material_directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.material_directory.cleanup)
        root = Path(cls.material_directory.name)
        key, cert, p12 = root / "key.pem", root / "cert.pem", root / "fixture.p12"
        commands = [
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=Apple Distribution/OU=A1B2C3D4E5"],
            ["openssl", "pkcs12", "-export", "-inkey", str(key), "-in", str(cert), "-out", str(p12), "-passout", "stdin"],
        ]
        for command in commands:
            subprocess.run(command, input=b" private password \n", capture_output=True, check=True)
        cls.p12_bytes = p12.read_bytes()
        cls.api_key_bytes = subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-pkeyopt", "ec_param_enc:named_curve"], capture_output=True, check=True).stdout

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "brands.json"
        self.config.write_text(json.dumps({"_iosTeams": {"example": {"teamId": "A1B2C3D4E5", "credentialNamespace": "ios-team-example"}}, "example": {"ios": {"signing": {"test": {"enabled": False, "team": "example"}}}}}))
        self.p12 = self.secret("distribution.p12", self.p12_bytes)
        self.password = self.secret("password", b" private password \n")
        self.auth = self.secret("auth.json", json.dumps({"username": "admin", "apiToken": "private-token"}).encode())
        self.key = self.secret("key.p8", self.api_key_bytes)
        self.api_config = self.secret("api.json", json.dumps({"keyPath": "key.p8", "keyId": "ABCDE12345", "issuerId": "11111111-2222-3333-4444-555555555555", "teamId": "A1B2C3D4E5"}).encode())
        self.controller = FakeController()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.controller.handler())
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:{}".format(self.server.server_address[1])
        self.client = credentials.JenkinsClient(self.url, credentials.auth_headers(self.auth))

    def secret(self, name, value):
        path = self.root / name
        path.write_bytes(value)
        path.chmod(0o600)
        return path

    def items(self, api=False):
        return credentials.credential_inputs(self.config, "example", self.p12, self.password, self.api_config if api else None)

    def test_descriptions_identify_team_purpose_and_consumer_without_materials(self):
        items = credentials.credential_inputs(self.config, "example")
        self.assertEqual(5, len(items))
        self.assertTrue(all(item["description"].startswith("iOS | Team example（A1B2C3D4E5） | ")
                            and "iOS 打包、签名维护消费" in item["description"] for item in items))
        self.assertIn("P12 密码", items[1]["description"])
        self.assertIn("Issuer ID", items[4]["description"])
        self.assertTrue(all("value" not in item for item in items))
        self.assertEqual(2, credentials.GROOVY.count("item.id, item.description"))

    def run_cli(self, extra=(), materials=True):
        argv = ["--config", str(self.config), "--brand", "example", "--jenkins-url", self.url, "--auth-file", str(self.auth)]
        if materials:
            argv += ["--p12", str(self.p12), "--password-file", str(self.password)]
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = credentials.main(argv + list(extra))
        self.assertNotIn("private", output.getvalue() + errors.getvalue())
        return code, output.getvalue(), errors.getvalue()

    def test_plan_never_mutates_and_does_not_send_local_secret(self):
        code, output, _ = self.run_cli()
        self.assertEqual(0, code)
        self.assertEqual(["missing", "missing"], [row["status"] for row in json.loads(output)])
        self.assertEqual([], self.controller.creates)
        self.assertEqual({}, self.controller.values)
        script = urllib.parse.parse_qs(self.controller.requests[0][2].decode())["script"][0]
        encoded = re.search(r"parseText\(new String\('([A-Za-z0-9+/=]+)'", script).group(1)
        payload = json.loads(base64.b64decode(encoded))
        self.assertFalse(payload["apply"])
        self.assertTrue(all("value" not in item for item in payload["items"]))

    def test_apply_creates_only_selected_pair_and_repeat_has_no_write_request(self):
        code, output, _ = self.run_cli(["--apply"])
        self.assertEqual(0, code)
        self.assertEqual(["created", "created"], [row["status"] for row in json.loads(output)])
        self.assertEqual(2, len(self.controller.creates))
        self.assertEqual(b" private password ", self.controller.values["ios-team-example-distribution-p12-password"][1])
        before = len(self.controller.requests)
        code, output, _ = self.run_cli(["--apply"])
        self.assertEqual(0, code)
        self.assertEqual(["matching", "matching"], [row["status"] for row in json.loads(output)])
        self.assertEqual(2, len(self.controller.creates))
        self.assertEqual(1, len(self.controller.requests) - before)

    def test_apply_all_five_and_partially_existing_inputs_skip_matching_item(self):
        items = self.items(api=True)
        existing = items[0]
        self.controller.values[existing["id"]] = (existing["type"], base64.b64decode(existing["value"]))
        code, output, _ = self.run_cli(["--api-key-config", str(self.api_config), "--apply"])
        self.assertEqual(0, code)
        self.assertEqual(5, len(self.controller.values))
        self.assertEqual(4, len(self.controller.creates))
        self.assertEqual("matching", json.loads(output)[0]["status"])
        self.assertTrue(all(row["status"] == "created" for row in json.loads(output)[1:]))

    def test_apply_api_only_uses_three_derived_ids(self):
        code, output, _ = self.run_cli(["--api-key-config", str(self.api_config), "--apply"], materials=False)
        self.assertEqual(0, code)
        self.assertEqual(["ios-team-example-" + suffix for suffix in ("asc-api-key", "asc-key-id", "asc-issuer-id")], self.controller.creates)
        self.assertTrue(all(row["status"] == "created" for row in json.loads(output)))

    def test_conflicting_content_or_type_blocks_every_create(self):
        for kind, value, expected in (("Secret file", b"other", "conflict"), ("Secret text", self.p12_bytes, "wrong-type")):
            with self.subTest(kind=kind):
                self.controller.values = {"ios-team-example-distribution-p12": (kind, value)}
                before = len(self.controller.requests)
                code, output, _ = self.run_cli(["--apply"])
                self.assertEqual(1, code)
                self.assertEqual(expected, json.loads(output)[0]["status"])
                self.assertEqual([], self.controller.creates)
                self.assertEqual(1, len(self.controller.requests) - before)

    def test_apply_rechecks_conflict_after_plan(self):
        self.controller.race_conflict = True
        code, output, _ = self.run_cli(["--apply"])
        self.assertEqual(1, code)
        self.assertEqual("conflict", json.loads(output)[0]["status"])
        self.assertEqual([], self.controller.creates)

    def test_running_build_and_queue_block_apply(self):
        for attribute in ("running", "queued"):
            with self.subTest(attribute=attribute):
                setattr(self.controller, attribute, True)
                code, _, errors = self.run_cli(["--apply"])
                self.assertEqual(1, code)
                self.assertIn("running build 或 queue", errors)
                self.assertEqual([], self.controller.creates)
                setattr(self.controller, attribute, False)

    def test_server_idle_recheck_blocks_race(self):
        self.controller.race_busy = True
        code, _, errors = self.run_cli(["--apply"])
        self.assertEqual(1, code)
        self.assertIn("重新运行只读核对", errors)
        self.assertEqual([], self.controller.creates)

    def test_unknown_post_result_has_no_retry_and_next_plan_recovers(self):
        self.controller.fail_apply = True
        code, _, errors = self.run_cli(["--apply"])
        self.assertEqual(1, code)
        self.assertIn("HTTP 500", errors)
        self.assertEqual(2, len(self.controller.creates))
        self.assertEqual(2, sum(method == "POST" for method, _, _ in self.controller.requests))
        code, output, _ = self.run_cli()
        self.assertEqual(0, code)
        self.assertTrue(all(row["status"] == "matching" for row in json.loads(output)))

    def test_raw_http_and_script_errors_never_escape(self):
        self.controller.fail_status = 403
        code, _, errors = self.run_cli()
        self.assertEqual(1, code)
        self.assertIn("HTTP 403", errors)
        self.controller.fail_status = None
        self.controller.malformed = True
        code, _, errors = self.run_cli()
        self.assertEqual(1, code)
        self.assertIn("原始响应已隐藏", errors)

    def test_redirect_does_not_forward_authorization(self):
        self.controller.redirect = "http://localhost:{}/stolen".format(self.server.server_address[1])
        with self.assertRaisesRegex(credentials.CredentialError, "重定向被拒绝"):
            self.client.request("/scriptText", "nothing")
        self.assertEqual(1, len(self.controller.requests))

    def test_without_materials_plan_reports_five_types_without_claiming_content_match(self):
        self.controller.values = {"ios-team-example-" + suffix: (kind, b"unknown") for suffix, kind in credentials.SUFFIXES.items()}
        code, output, _ = self.run_cli(materials=False)
        self.assertEqual(0, code)
        self.assertEqual(5, len(json.loads(output)))
        self.assertTrue(all(row["status"] == "present-unverified" for row in json.loads(output)))
        code, _, _ = self.run_cli(["--apply"], materials=False)
        self.assertEqual(1, code)
        self.assertEqual([], self.controller.creates)

    def test_input_permissions_symlink_and_owner_are_enforced(self):
        self.p12.chmod(0o644)
        with self.assertRaisesRegex(credentials.CredentialError, "0600"):
            self.items()
        self.p12.chmod(0o600)
        link = self.root / "link"
        link.symlink_to(self.p12)
        with self.assertRaisesRegex(credentials.CredentialError, "符号链接"):
            credentials.private_bytes(link, "P12")
        with patch.object(credentials.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(credentials.CredentialError, "当前用户"):
                self.items()

    def test_readonly_jenkins_binding_is_explicit_and_preserves_other_guards(self):
        self.p12.chmod(0o400)
        with self.assertRaisesRegex(credentials.CredentialError, "0600"):
            credentials.private_bytes(self.p12, "P12")
        self.assertEqual(self.p12.read_bytes(), credentials.private_bytes(self.p12, "P12", allowed_modes=(0o400, 0o600)))
        for mode in (0o440, 0o644):
            self.p12.chmod(mode)
            with self.assertRaises(credentials.CredentialError):
                credentials.private_bytes(self.p12, "P12", allowed_modes=(0o400, 0o600))
        self.p12.chmod(0o400)
        link = self.root / "readonly-link"
        link.symlink_to(self.p12)
        with self.assertRaisesRegex(credentials.CredentialError, "符号链接"):
            credentials.private_bytes(link, "P12", allowed_modes=(0o400, 0o600))
        with patch.object(credentials.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(credentials.CredentialError, "当前用户"):
                credentials.private_bytes(self.p12, "P12", allowed_modes=(0o400, 0o600))

    def test_team_and_local_api_config_mismatch_are_rejected(self):
        config = json.loads(self.api_config.read_text())
        config["teamId"] = "WRONGTEAM1"
        self.api_config.write_text(json.dumps(config))
        with self.assertRaisesRegex(credentials.CredentialError, "Team 不一致"):
            self.items(api=True)
        with self.assertRaisesRegex(credentials.CredentialError, "未正确登记"):
            credentials.credential_inputs(self.config, "missing")

    def test_invalid_password_expired_identity_and_other_team_fail_before_http(self):
        self.password.write_bytes(b"wrong password")
        code, _, errors = self.run_cli(["--apply"])
        self.assertEqual(1, code)
        self.assertIn("校验失败", errors)
        self.assertEqual([], self.controller.requests)
        self.password.write_bytes(b" private password ")
        for identity, message in (({"teamId": "OTHERTEAM1", "expires": datetime.now(timezone.utc) + timedelta(days=1)}, "Team"),
                                  ({"teamId": "A1B2C3D4E5", "expires": datetime.now(timezone.utc) - timedelta(days=1)}, "过期")):
            with patch.object(credentials, "p12_identity", return_value=(b"unused-key", identity)):
                code, _, errors = self.run_cli(["--apply"])
            self.assertEqual(1, code)
            self.assertIn(message, errors)
            self.assertEqual([], self.controller.requests)

    def test_non_p256_api_key_is_rejected_before_http(self):
        self.key.write_bytes(subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:secp384r1", "-pkeyopt", "ec_param_enc:named_curve"], capture_output=True, check=True).stdout)
        code, _, errors = self.run_cli(["--api-key-config", str(self.api_config), "--apply"], materials=False)
        self.assertEqual(1, code)
        self.assertIn("P-256", errors)
        self.assertEqual([], self.controller.requests)

    def test_auth_helper_executes_argv_without_shell_or_output_leak(self):
        helper = {"headers": {"Authorization": "Basic private-token"}}
        with patch.object(credentials.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(helper).encode(), b"")) as run:
            headers = credentials.auth_headers(headers_command="'/trusted helper' --argument value")
        self.assertEqual(helper["headers"], headers)
        self.assertEqual(["/trusted helper", "--argument", "value"], run.call_args.args[0])
        self.assertNotIn("shell", run.call_args.kwargs)
        with patch.object(credentials.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "helper", output=b"private", stderr=b"private")):
            with self.assertRaisesRegex(credentials.CredentialError, "输出已隐藏") as error:
                credentials.auth_headers(headers_command="helper")
        self.assertNotIn("private", str(error.exception))

    def test_rejects_auth_header_injection_and_unsafe_url(self):
        with patch.object(credentials.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b'{"Authorization":"Basic x\\r\\nInjected: x"}', b"")):
            with self.assertRaises(credentials.CredentialError):
                credentials.auth_headers(headers_command="helper")
        for url in ("https://admin:private@example.org", "https://example.org?private=x", "file:///private", "http://[broken", "http://example.org:broken"):
            with self.assertRaises(credentials.CredentialError):
                credentials.JenkinsClient(url, {})


if __name__ == "__main__":
    unittest.main()
