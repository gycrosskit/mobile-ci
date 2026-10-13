import io
import json
from email.message import Message
from http.client import IncompleteRead
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError
from urllib.response import addinfourl

from delivery.dingtalk_notify import (
    BrandConfig,
    DingTalkError,
    DingTalkRejectedError,
    _post_payload,
    _send_with_retry,
    Tester,
    build_failure_markdown,
    build_failure_payload,
    build_health_markdown,
    build_health_payload,
    build_markdown,
    build_payload,
    failure_reason_for_stage,
    load_brand_config,
    send_release_notification,
    send_failure_notification,
    send_health_notification,
)
from delivery.delivery import ArtifactInfo, DeliveryContextError, WorkItemRef, write_json_object


class DingTalkNotifyTest(unittest.TestCase):
    def test_webhook_error_hides_response_and_invalid_code(self):
        sentinel = "FAKE_REFLECTED_CREDENTIAL"
        for code in (310000, sentinel):
            with self.subTest(code=code):
                response = MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps({"errcode": code, "errmsg": sentinel}).encode()
                with patch("delivery.dingtalk_notify.urlopen", return_value=response):
                    with self.assertRaises(DingTalkError) as caught:
                        _post_payload("https://example.test", "fixture", {}, 1)
                self.assertNotIn(sentinel, str(caught.exception))
                if code == 310000:
                    self.assertIn("310000", str(caught.exception))

    def test_webhook_rejects_non_object_json(self):
        for body in (b"[]", b"null", b'"invalid"'):
            with self.subTest(body=body):
                response = MagicMock()
                response.__enter__.return_value.read.return_value = body
                with patch("delivery.dingtalk_notify.urlopen", return_value=response):
                    with self.assertRaisesRegex(DingTalkError, "无效数据结构"):
                        _post_payload("https://example.test", "test-secret", {}, 1)

    def setUp(self):
        self.state_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_directory.cleanup)
        self.state_root = Path(self.state_directory.name).resolve()
        self.environment = patch.dict(os.environ, {"DELIVERY_STATE_ROOT": str(self.state_root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.brand = BrandConfig(
            key="miaoshou",
            display_name="妙手中医",
            testers=[Tester(name="测试A", mobile="13800000000")],
            pgyer_channels={},
        )
        self.release = ArtifactInfo(
            name="妙手中医",
            version="3.2.1",
            version_code="321",
            package_name="com.example.app",
            build_key="key",
            download_url="https://www.pgyer.com/abcd",
            qr_code_url="https://example.test/qr",
        )

    def notify(self, **kwargs):
        values = dict(release=self.release, brand=self.brand, changelog="- 更新",
                      environment="test", branch="dev/miaoshou", commit="abc123", builder="jenkins",
                      webhook="https://example.test?token=fixture-token", secret="fixture-secret",
                      retry_delays=(0, 0), artifact_sha256="a" * 64)
        values.update(kwargs)
        return send_release_notification(**values)

    def test_sent_release_is_reused_without_storing_message_or_credentials(self):
        with patch("delivery.dingtalk_notify._post_payload") as post:
            first = self.notify()
            self.assertEqual(first, self.notify())
        self.assertEqual(1, post.call_count)
        stored = "".join(path.read_text() for path in self.state_root.rglob("*.json"))
        self.assertIn('"sent"', stored)
        for value in ("fixture-token", "fixture-secret", "13800000000", "- 更新"):
            self.assertNotIn(value, stored)

    def test_unknown_receipt_status_never_resends(self):
        with patch("delivery.dingtalk_notify._post_payload") as post:
            self.notify()
            path = next(self.state_root.glob("*.json"))
            state = json.loads(path.read_text())
            for fields in ({"status": "invalid-fixture"}, {"status": None}, {}, {"phase": "pending"}):
                with self.subTest(fields=fields):
                    invalid = {key: value for key, value in state.items() if key != "status"}
                    invalid.update(fields)
                    path.write_text(json.dumps(invalid))
                    with self.assertRaises((DingTalkError, DeliveryContextError)):
                        self.notify()
        self.assertEqual(1, post.call_count)

    def test_unknown_post_and_interruption_keep_pending_and_block_restart(self):
        for index, error in enumerate((DingTalkError("response lost"), KeyboardInterrupt())):
            with self.subTest(error=type(error).__name__), \
                    patch("delivery.dingtalk_notify._post_payload", side_effect=error) as post:
                identity = {"release": self.release._replace(build_key="unknown-" + str(index))}
                with self.assertRaises(type(error)):
                    self.notify(**identity)
                with self.assertRaisesRegex(DingTalkError, "结果未知"):
                    self.notify(**identity)
                self.assertEqual(1, post.call_count)

    def test_sent_receipt_write_failure_keeps_pending_and_restart_never_reposts(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"errcode":0}'
        def fail_sent_write(path, value):
            if value["status"] == "sent":
                raise OSError("fixture persistence failure")
            write_json_object(path, value)
        with patch("delivery.dingtalk_notify.urlopen", return_value=response) as post:
            with patch("delivery.delivery.write_json_object", side_effect=fail_sent_write) as save:
                with self.assertRaises(OSError):
                    self.notify()
                self.assertEqual(2, save.call_count)
            state = json.loads(next(self.state_root.glob("*.json")).read_text())
            self.assertEqual("pending", state["status"])
            with self.assertRaisesRegex(DingTalkError, "结果未知"):
                self.notify()
        self.assertEqual(1, post.call_count)

    def test_invalid_response_never_retries_or_reposts(self):
        for index, body in enumerate((b"not-json", b"[]", b"{}", b'{"errcode":true}')):
            with self.subTest(body=body):
                response = MagicMock()
                response.__enter__.return_value.read.return_value = body
                with patch("delivery.dingtalk_notify.urlopen", return_value=response) as post:
                    with self.assertRaises(DingTalkError):
                        self.notify(platform="fixture-" + str(index))
                    with self.assertRaisesRegex(DingTalkError, "结果未知"):
                        self.notify(platform="fixture-" + str(index))
                self.assertEqual(1, post.call_count)

    def test_explicit_rejection_retries_finitely_and_allows_later_manual_retry(self):
        with patch("delivery.dingtalk_notify._post_payload", side_effect=DingTalkRejectedError("rejected")) as post, \
                patch("delivery.dingtalk_notify.time.sleep") as sleep:
            with self.assertRaises(DingTalkRejectedError):
                self.notify()
        self.assertEqual(3, post.call_count)
        self.assertEqual(2, sleep.call_count)
        with patch("delivery.dingtalk_notify._post_payload") as post:
            self.notify()
            self.notify()
        self.assertEqual(1, post.call_count)

    def test_local_credential_validation_does_not_create_pending(self):
        for fields in ({"webhook": ""}, {"secret": ""}, {"webhook": "http://example.test"},
                       {"artifact_sha256": ""}, {"artifact_sha256": "bad"}):
            with self.subTest(fields=fields), patch("delivery.dingtalk_notify._post_payload") as post:
                with self.assertRaises(DingTalkError):
                    self.notify(**fields)
                post.assert_not_called()
        self.assertEqual([], list(self.state_root.iterdir()))

    def test_http_and_network_outcomes_are_unknown_and_do_not_retry(self):
        for index, error in enumerate((HTTPError("https://example.test", 503, "fixture", {}, None),
                                       URLError("fixture"), TimeoutError(), OSError("fixture"), IncompleteRead(b"fixture"))):
            with self.subTest(error=type(error).__name__), \
                    patch("delivery.dingtalk_notify.urlopen", side_effect=error) as post:
                with self.assertRaises(DingTalkError):
                    self.notify(platform="network-" + str(index))
                with self.assertRaisesRegex(DingTalkError, "结果未知"):
                    self.notify(platform="network-" + str(index))
                self.assertEqual(1, post.call_count)

    def test_different_build_is_distinct_but_same_logical_account_token_rotation_is_not(self):
        with patch("delivery.dingtalk_notify._post_payload") as post:
            self.notify()
            self.notify(release=self.release._replace(build_key="another-build"), artifact_sha256="b" * 64)
            self.notify(webhook="https://example.test?token=another-fixture")
        self.assertEqual(2, post.call_count)

    def test_same_logical_account_token_rotation_cannot_bypass_unknown_notification(self):
        with patch("delivery.dingtalk_notify._send_with_retry", side_effect=DingTalkError("unknown")) as send:
            with self.assertRaises(DingTalkError): self.notify()
            with self.assertRaisesRegex(DingTalkError, "结果未知"):
                self.notify(webhook="https://example.test?token=rotated-fixture")
            self.assertEqual(1, send.call_count)

    def test_same_build_rejects_changed_material_after_pending_or_sent(self):
        for status in ("pending", "sent"):
            release = self.release._replace(build_key="material-" + status)
            error = DingTalkError("unknown") if status == "pending" else None
            with self.subTest(status=status), patch("delivery.dingtalk_notify._post_payload", side_effect=error) as post:
                if status == "pending":
                    with self.assertRaises(DingTalkError):
                        self.notify(release=release)
                else:
                    self.notify(release=release)
                for fields in ({"artifact_sha256": "b" * 64}, {"branch": "other"},
                               {"commit": "other"}, {"ui_engine": "kuikly"}, {"environment": "release"},
                               {"brand": self.brand._replace(key="other")},
                               {"release": release._replace(version="9.9.9")}):
                    with self.subTest(fields=tuple(fields)), self.assertRaises(DeliveryContextError):
                        self.notify(**{**{"release": release}, **fields})
                self.assertEqual(1, post.call_count)

    def test_app_store_constant_keys_allow_new_versions_but_bind_same_version_material(self):
        for distribution, key in (("app-store-connect", "APP-STORE-CONNECT"), ("testflight", "TESTFLIGHT")):
            release = self.release._replace(build_key=key)
            with self.subTest(distribution=distribution), patch("delivery.dingtalk_notify._post_payload") as post:
                self.notify(release=release, platform="iOS", distribution=distribution)
                self.notify(release=release, platform="iOS", distribution=distribution)
                with self.assertRaises(DeliveryContextError):
                    self.notify(release=release, platform="iOS", distribution=distribution, artifact_sha256="b" * 64)
                self.notify(release=release._replace(version_code="322"), platform="iOS", distribution=distribution)
                self.notify(release=release._replace(package_name="com.example.other"), platform="iOS", distribution=distribution)
                self.assertEqual(3, post.call_count)

    def test_redirect_never_follows_get_or_retries_original_post(self):
        for code in (301, 302, 303, 307, 308):
            for get_errcode in (0, 310000):
                calls = []
                def fixture_https(request):
                    method = request.get_method()
                    calls.append(method)
                    headers = Message()
                    if method == "POST":
                        headers["Location"] = "https://example.test/redirect-result"
                        status, body = code, b""
                    else:
                        status, body = 200, json.dumps({"errcode": get_errcode}).encode()
                    response = addinfourl(io.BytesIO(body), headers, request.full_url, status)
                    response.msg = "fixture"
                    return response
                release = self.release._replace(build_key="redirect-{}-{}".format(code, get_errcode))
                with self.subTest(code=code, get_errcode=get_errcode), \
                        patch("urllib.request.HTTPSHandler.https_open", side_effect=fixture_https):
                    with self.assertRaises(DingTalkError):
                        self.notify(release=release)
                    with self.assertRaisesRegex(DingTalkError, "结果未知"):
                        self.notify(release=release)
                self.assertEqual(["POST"], calls)

    def test_waiting_sender_never_reposts_after_unknown_or_sent_save_failure(self):
        for sent_save_failure in (False, True):
            release = self.release._replace(build_key="waiting-" + str(sent_save_failure))
            at_post, allow_result = threading.Event(), threading.Event()
            waiter_started, waiter_finished = threading.Event(), threading.Event()
            calls, errors = [], []
            def post(*args):
                calls.append("POST")
                at_post.set()
                if not allow_result.wait(5):
                    raise AssertionError("fixture sender did not resume")
                if not sent_save_failure:
                    raise DingTalkError("unknown")
            def save(path, value):
                if sent_save_failure and value["status"] == "sent":
                    raise OSError("fixture persistence failure")
                write_json_object(path, value)
            def worker(waiter=False):
                if waiter:
                    waiter_started.set()
                try:
                    self.notify(release=release)
                except (DingTalkError, OSError) as error:
                    errors.append(type(error))
                finally:
                    if waiter:
                        waiter_finished.set()
            with self.subTest(sent_save_failure=sent_save_failure), \
                    patch("delivery.dingtalk_notify._post_payload", side_effect=post), \
                    patch("delivery.delivery.write_json_object", side_effect=save):
                sender, waiter = threading.Thread(target=worker), threading.Thread(target=worker, args=(True,))
                sender.start()
                try:
                    self.assertTrue(at_post.wait(5))
                    waiter.start()
                    self.assertTrue(waiter_started.wait(5))
                    self.assertFalse(waiter_finished.wait(0.05))
                finally:
                    allow_result.set()
                    sender.join(5)
                    if waiter.ident is not None:
                        waiter.join(5)
                self.assertFalse(sender.is_alive())
                self.assertFalse(waiter.is_alive())
                self.assertEqual(["POST"], calls)
                self.assertCountEqual([OSError if sent_save_failure else DingTalkError, DingTalkError], errors)
                with self.assertRaisesRegex(DingTalkError, "结果未知"):
                    self.notify(release=release)

    def test_two_processes_send_the_same_release_once(self):
        calls = self.state_root / "post-calls"
        context = multiprocessing.get_context("fork")
        ready = context.Barrier(2)
        def worker():
            def record(*args):
                with calls.open("a") as output:
                    output.write("post\n")
                time.sleep(0.05)
            ready.wait(timeout=5)
            with patch("delivery.dingtalk_notify._post_payload", side_effect=record):
                self.notify()
        processes = [context.Process(target=worker) for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join()
                self.fail("notification process did not finish")
            self.assertEqual(0, process.exitcode)
        self.assertEqual(["post"], calls.read_text().splitlines())

    def test_failure_and_health_alerts_stop_on_unknown_response_without_global_state(self):
        with patch("delivery.dingtalk_notify._post_payload", side_effect=DingTalkError("unknown")) as post:
            with self.assertRaises(DingTalkError):
                send_failure_notification("job", "1", "FAILURE", "Build APK", "Android", "miaoshou",
                                          "master", "https://example.test/job/1", "https://example.test", "secret",
                                          retry_delays=(0, 0))
            with self.assertRaises(DingTalkError):
                send_health_notification("down", "fixture", "https://example.test", "https://example.test",
                                         "secret", retry_delays=(0, 0))
        self.assertEqual(2, post.call_count)
        self.assertEqual([], list(self.state_root.iterdir()))

    def test_retry_accepts_only_explicit_business_rejections(self):
        with patch("delivery.dingtalk_notify._post_payload", side_effect=[DingTalkRejectedError("rejected"), None]) as post, \
                patch("delivery.dingtalk_notify.time.sleep"):
            _send_with_retry("fixture", "webhook", "secret", {}, 1, (0, 0))
        self.assertEqual(2, post.call_count)

    def test_markdown_and_payload_contain_real_at_mobile(self):
        markdown = build_markdown(
            self.release,
            self.brand,
            "- fix: 修复登录",
            "test",
            "release/miaoshou-3.2.1",
            "a87c31f012",
            "guoyang",
        )
        payload = build_payload(markdown, self.brand)

        self.assertIn("3.2.1 (321)", markdown)
        self.assertIn("@13800000000", markdown)
        self.assertEqual(["13800000000"], payload["at"]["atMobiles"])

    def test_preview_masks_mobile(self):
        markdown = build_markdown(
            self.release,
            self.brand,
            "- fix: 修复登录",
            "test",
            "release/miaoshou-3.2.1",
            "a87c31f012",
            "guoyang",
            mask_mobiles=True,
        )

        self.assertIn("@138****0000", markdown)
        self.assertNotIn("13800000000", markdown)

    def test_notification_includes_selected_ui_engine(self):
        for engine, label in (("cmp", "CMP"), ("kuikly", "Kuikly")):
            with self.subTest(engine=engine), patch("delivery.dingtalk_notify._send_with_retry"):
                markdown = send_release_notification(
                    self.release._replace(build_key="engine-" + engine), self.brand, "- 更新", "test", "dev/miaoshou",
                    "abc123", "jenkins", "https://example.test", "secret", ui_engine=engine,
                    artifact_sha256="a" * 64,
                )
                self.assertIn("**UI 引擎：** " + label, markdown)

    def test_release_environment_uses_formal_package_label(self):
        markdown = build_markdown(
            self.release,
            self.brand,
            "- feat: 正式发布",
            "release",
            "miaoshou-v3.2.1",
            "a87c31f012",
            "guoyang",
        )

        self.assertIn("Android 正式发布包", markdown)

    def test_ios_app_store_notification_has_no_pgyer_placeholder(self):
        markdown = build_markdown(
            self.release,
            self.brand,
            "- feat: iOS 正式发布",
            "prod",
            "kangmin-ios-v1.0.0",
            "a87c31f012",
            "jenkins",
            platform="iOS",
            distribution="app-store-connect",
        )

        self.assertIn("iOS 正式发布包", markdown)
        self.assertIn("App Store Connect", markdown)
        self.assertNotIn("蒲公英发布完成", markdown)

    def test_ios_testflight_notification_remains_a_test_package(self):
        markdown = build_markdown(
            self.release,
            self.brand,
            "- feat: iOS TestFlight 测试",
            "dev",
            "feature/miaoshou/testflight",
            "a87c31f012",
            "jenkins",
            platform="iOS",
            distribution="testflight",
        )

        self.assertIn("iOS 版本测试包", markdown)
        self.assertIn("TestFlight", markdown)
        self.assertNotIn("蒲公英发布完成", markdown)

    def test_markdown_contains_linked_zentao_work_item(self):
        markdown = build_markdown(
            self.release,
            self.brand,
            "- feat: 关联工作项",
            "test",
            "feature/miaoshou/work-item",
            "a87c31f012",
            "guoyang",
            work_item=WorkItemRef(
                system="zentao",
                item_type="bug",
                item_id="2048",
                url="https://tracker.example.invalid/zentao/bug-view-2048.html",
            ),
        )

        self.assertIn("关联禅道", markdown)
        self.assertIn("[Bug #2048]", markdown)
        self.assertIn("https://tracker.example.invalid/zentao/bug-view-2048.html", markdown)

    @patch.dict(os.environ, {"DINGTALK_TESTER_MOBILE": "13800000000"})
    def test_loads_default_tester_mobile_from_environment(self):
        config = {
            "_defaults": {
                "testers": [
                    {"name": "测试A", "mobileEnv": "DINGTALK_TESTER_MOBILE"}
                ]
            },
            "miaoshou": {"displayName": "妙手中医", "pgyerChannels": {}},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brands.json"
            path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

            brand = load_brand_config(path, "miaoshou")

        self.assertEqual("测试A", brand.testers[0].name)
        self.assertEqual("13800000000", brand.testers[0].mobile)

    def test_failure_markdown_contains_stage_reason_and_build_link(self):
        markdown = build_failure_markdown(
            job="sxmqlive-alert-test",
            build_number="42",
            result="FAILURE",
            stage="Controlled Failure",
            platform="Operations",
            brand="",
            git_ref="master",
            build_url="https://jenkins.example.test/job/alert-test/42/?token=secret",
        )

        self.assertIn("Jenkins 构建失败", markdown)
        self.assertIn("管理员主动执行 Jenkins 失败告警验收", markdown)
        self.assertIn("[sxmqlive-alert-test #42](https://jenkins.example.test/job/alert-test/42/)", markdown)
        self.assertNotIn("token=secret", markdown)
        self.assertIn("Operations / 不适用", markdown)

    def test_failure_markdown_flattens_untrusted_jenkins_fields(self):
        markdown = build_failure_markdown(
            job="bad\n### injected",
            build_number="1",
            result="UNSTABLE",
            stage="unknown\n[link](https://attacker.test)",
            platform="Android",
            brand="kangmin",
            git_ref="feature/kangmin/demo",
            build_url="https://user:password@jenkins.example.test/job/1/",
        )

        self.assertIn("Jenkins 构建不稳定", markdown)
        self.assertNotIn("\n### injected", markdown)
        self.assertNotIn("attacker.test", markdown)
        self.assertNotIn("password", markdown)
        self.assertEqual(
            "流水线执行失败，请打开 Jenkins 构建日志查看具体错误",
            failure_reason_for_stage("unknown"),
        )

    def test_failure_payload_does_not_at_people(self):
        payload = build_failure_payload("failure", "FAILURE")

        self.assertEqual("Jenkins 构建失败", payload["markdown"]["title"])
        self.assertEqual([], payload["at"]["atMobiles"])
        self.assertFalse(payload["at"]["isAtAll"])

    def test_health_markdown_reports_recovery_without_query_string(self):
        markdown = build_health_markdown(
            "healthy",
            "Controller 与 Agent 正常",
            "https://jenkins.example.test/?token=secret",
            "down",
        )
        payload = build_health_payload(markdown, "healthy")

        self.assertIn("Jenkins 已恢复", markdown)
        self.assertIn("DOWN → HEALTHY", markdown)
        self.assertNotIn("token=secret", markdown)
        self.assertEqual("Jenkins 已恢复", payload["markdown"]["title"])


if __name__ == "__main__":
    unittest.main()
