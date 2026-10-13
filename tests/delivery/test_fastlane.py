import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

class FastlaneTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ruby"), "Fastlane 行为测试需要 Ruby")
    def test_normal_build_downloads_profile_readonly_and_maintenance_can_refresh(self):
        # 执行真实 Fastfile，替换外部 Apple 调用；不需要登录或安装 Fastlane。
        driver = r'''
require "json"
$lanes = {}
def fastlane_version(*); end
def default_platform(*); end
def desc(*); end
def platform(*) ; yield; end
def lane(name, &block); $lanes[name] = block; end
def app_store_connect_api_key(*); {}; end
def register_device(*); $registered = true; end
def get_provisioning_profile(options)
  raise "Conflicting readonly/force options" if options.key?(:readonly) && options.key?(:force)
  $profile = options
end
load ARGV[0]
$lanes[ARGV[1].to_sym].call
puts JSON.generate({ readonly: !!$profile[:readonly], force: !!$profile[:force], registered: !!$registered })
'''
        env = dict(os.environ, ASC_API_KEY_ID="test", ASC_ISSUER_ID="test", ASC_API_KEY_PATH="test",
                   IOS_BUNDLE_ID="com.example.app", IOS_TEAM_ID="TESTTEAM01", IOS_PROFILE_NAME="Test Profile",
                   IOS_PROFILE_OUTPUT_PATH="test", NEW_DEVICE_NAME="test", NEW_DEVICE_UDID="test")
        for register, refresh in ((False, False), (True, False), (False, True)):
            with self.subTest(register=register, refresh=refresh):
                result = subprocess.run(["ruby", "-e", driver, str(ROOT / "delivery/fastlane/Fastfile"), "sync_adhoc_profile"],
                                        env=dict(env, REGISTER_NEW_DEVICE=str(register).lower(),
                                                 FORCE_PROFILE_REFRESH=str(refresh).lower()),
                                        capture_output=True, text=True, check=True)
                profile = json.loads(result.stdout)
                self.assertEqual(not (register or refresh), profile["readonly"])
                self.assertEqual(register or refresh, profile["force"])
                self.assertEqual(register, profile["registered"])
        result = subprocess.run(["ruby", "-e", driver, str(ROOT / "delivery/fastlane/Fastfile"), "download_appstore_profile"],
                                env=env, capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(result.stdout)["readonly"])
