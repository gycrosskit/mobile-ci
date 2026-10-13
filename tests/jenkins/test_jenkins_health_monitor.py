import tempfile
import io
import json
from email.message import Message
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.response import addinfourl

from jenkins.jenkins_health_monitor import load_state, save_state, should_notify
from jenkins import jenkins_health_monitor as monitor


class JenkinsHealthMonitorTest(unittest.TestCase):
    def test_http_errors_cannot_report_controller_healthy(self):
        for code, expected in ((301, "degraded"), (302, "degraded"), (307, "degraded"),
                               (401, "degraded"), (403, "degraded"), (404, "degraded"),
                               (429, "degraded"), (503, "down")):
            with self.subTest(code=code), patch.object(
                monitor, "urlopen", side_effect=HTTPError("https://jenkins.test/login", code, "", {}, None),
            ):
                status, summary = monitor.inspect_jenkins("https://jenkins.test", 1)
                self.assertEqual(expected, status)
                self.assertIn(str(code), summary)

    def test_authenticated_redirects_do_not_send_credentials_to_another_host_or_plain_http(self):
        for code in (301, 302, 303, 307, 308):
            for target in ('https://other.test/collect', 'http://jenkins.test/collect'):
                with self.subTest(code=code, target=target):
                    requests = []
                    def transport(handler, request):
                        requests.append(request)
                        headers = Message()
                        headers['Location'] = target
                        response = addinfourl(io.BytesIO(b'ignored redirect body'), headers, request.full_url, code)
                        response.msg = 'redirect'
                        return response
                    with patch('urllib.request.HTTPSHandler.https_open', transport), \
                            patch('urllib.request.HTTPHandler.http_open', transport):
                        with self.assertRaises(HTTPError) as error:
                            monitor._request_json('https://jenkins.test/computer/api/json', 1, 'fixture-user', 'fixture-token')
                    self.assertEqual(code, error.exception.code)
                    self.assertEqual(['https://jenkins.test/computer/api/json'], [request.full_url for request in requests])
                    self.assertTrue(requests[0].get_header('Authorization').startswith('Basic '))

    def test_valid_local_http_json_still_works_without_redirect(self):
        payload = {'computer': []}
        def transport(handler, request):
            self.assertEqual('http://127.0.0.1:18080/computer/api/json', request.full_url)
            response = addinfourl(io.BytesIO(json.dumps(payload).encode()), Message(), request.full_url, 200)
            response.msg = 'OK'
            return response
        with patch('urllib.request.HTTPHandler.http_open', transport):
            self.assertEqual(payload, monitor._request_json('http://127.0.0.1:18080/computer/api/json', 1, 'fixture-user', 'fixture-token'))

    def test_agent_payload_must_contain_valid_statuses(self):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        valid = {"displayName": "agent", "offline": False, "temporarilyOffline": False}
        for payload in (None, [], {}, {"computer": None}, {"computer": [None]},
                        {"computer": [{}]}, {"computer": [{**valid, "offline": "false"}]}):
            with self.subTest(payload=payload), patch.object(monitor, "urlopen", return_value=response), \
                 patch.object(monitor, "_request_json", return_value=payload):
                status, _ = monitor.inspect_jenkins("https://jenkins.test", 1, "user", "test-token")
                self.assertEqual("degraded", status)
        with patch.object(monitor, "urlopen", return_value=response), \
             patch.object(monitor, "_request_json", return_value={"computer": [valid]}):
            status, _ = monitor.inspect_jenkins("https://jenkins.test", 1, "user", "test-token")
            self.assertEqual("healthy", status)

    def test_first_healthy_check_does_not_notify(self):
        self.assertFalse(should_notify({}, "healthy", 1000, 300))

    def test_first_failure_and_transition_notify(self):
        self.assertTrue(should_notify({}, "down", 1000, 300))
        self.assertTrue(should_notify({"status": "down"}, "healthy", 1000, 300))

    def test_repeated_failure_uses_interval(self):
        previous = {"status": "down", "lastNotifiedAt": 900}
        self.assertFalse(should_notify(previous, "down", 1000, 300))
        self.assertTrue(should_notify(previous, "down", 1200, 300))

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            value = {"status": "degraded", "checkedAt": 123}
            save_state(path, value)
            self.assertEqual(load_state(path), value)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_dry_run_preserves_pending_failure_and_recovery_notifications(self):
        for previous, status in (({}, "down"), ({"status": "down", "lastNotifiedAt": 900}, "healthy")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "state.json"
                save_state(path, previous)
                original = path.read_bytes()
                with patch.object(monitor, "inspect_jenkins", return_value=(status, "test")), \
                     patch.object(monitor, "send_health_notification") as send, \
                     patch.object(monitor.time, "time", return_value=1000):
                    monitor.main(["--state-file", str(path), "--dry-run"])
                    self.assertEqual(original, path.read_bytes())
                    send.assert_not_called()
                    monitor.main(["--state-file", str(path)])
                    send.assert_called_once()
                    self.assertEqual(1000, load_state(path)["lastNotifiedAt"])


if __name__ == "__main__":
    unittest.main()
