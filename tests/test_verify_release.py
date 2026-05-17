import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import verify_release


class VerifyReleaseTests(unittest.TestCase):
    def test_build_release_summary_groups_pass_warning_and_blocking(self):
        report = {
            "checks": {
                "exe": {"exists": True},
                "base_library": {"exists": True},
                "ffmpeg": {"exists": False},
                "ffprobe": {"exists": True},
                "skill_wrapper": {"exists": True},
            },
            "worker_help": {"ok": True},
            "worker_preflight": {"ok": False, "payload": {"fatal_errors": ["缺少模型配置"]}},
            "system_draft_resolution": {
                "ok": True,
                "warnings": ["当前回退到便携草稿目录: D:/portable/drafts"],
            },
            "gui_smoke_test": {"still_running_after_8s": True},
            "smoke_generation": {
                "ok": False,
                "blocking_issues": ["worker 烟测输出中包含 ERROR 级别异常。"],
                "warnings": ["worker 烟测产生了 stderr 输出，请复核是否存在非致命环境告警。"],
            },
        }

        summary = verify_release.build_release_summary(report)

        self.assertIn("主程序存在", summary["passed"])
        self.assertIn("GUI 烟测", summary["passed"])
        self.assertTrue(any("ffmpeg 存在 缺失" == item for item in summary["blocking"]))
        self.assertTrue(any("worker --preflight: 缺少模型配置" == item for item in summary["blocking"]))
        self.assertTrue(any("草稿目录解析: 当前回退到便携草稿目录" in item for item in summary["warnings"]))
        self.assertTrue(any("worker 生成烟测: worker 烟测产生了 stderr 输出" in item for item in summary["warnings"]))

    def test_print_release_summary_outputs_grouped_sections(self):
        report = {
            "ok": False,
            "checks": {
                "exe": {"exists": True},
                "base_library": {"exists": True},
                "ffmpeg": {"exists": True},
                "ffprobe": {"exists": True},
                "skill_wrapper": {"exists": True},
            },
            "worker_help": {"ok": True},
            "worker_preflight": {"ok": True},
            "system_draft_resolution": {"ok": True, "warnings": ["当前回退到便携草稿目录: D:/portable/drafts"]},
            "gui_smoke_test": {"still_running_after_8s": False},
            "smoke_generation": {"ok": True},
        }

        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            verify_release.print_release_summary(Path("release_verification.json"), report)

        output = mock_stdout.getvalue()
        self.assertIn("[结果] 失败", output)
        self.assertIn("[通过项]", output)
        self.assertIn("[警告]", output)
        self.assertIn("[阻断项]", output)
        self.assertIn("GUI 烟测 未通过", output)

    def test_contains_blocking_smoke_issue_treats_plain_stderr_as_warning(self):
        blocking, warnings = verify_release._contains_blocking_smoke_issue(
            stdout_text="worker completed",
            stderr_text="font cache warming",
        )

        self.assertEqual(blocking, [])
        self.assertTrue(any("stderr" in item for item in warnings))

    def test_contains_blocking_smoke_issue_blocks_traceback(self):
        blocking, warnings = verify_release._contains_blocking_smoke_issue(
            stdout_text="",
            stderr_text="Traceback (most recent call last):\nRuntimeError: boom",
        )

        self.assertTrue(any("Traceback" in item for item in blocking))
        self.assertTrue(any("stderr" in item for item in warnings))

    @patch("verify_release.subprocess.run")
    def test_verify_system_draft_resolution_accepts_portable_fallback(self, mock_run):
        mock_run.return_value = SimpleNamespace(
            returncode=1,
            stdout=(
                '{"checks":{"draft_root":"D:/portable/drafts",'
                '"official_draft_root":"C:/Users/demo/AppData/Local/JianyingPro/User Data/Projects/com.lveditor.draft",'
                '"using_portable_draft_root":true}}'
            ),
            stderr="",
        )

        result = verify_release.verify_system_draft_resolution(Path("demo.exe"), Path("dist"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["resolved_mode"], "portable")
        self.assertTrue(any("便携草稿目录" in item for item in result["warnings"]))

    @patch("verify_release.Path.home", return_value=Path(r"C:\Users\demo"))
    @patch("verify_release.subprocess.run")
    def test_verify_system_draft_resolution_accepts_expected_system_root(self, mock_run, _mock_home):
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout=(
                '{"checks":{"draft_root":"C:/Users/demo/AppData/Local/JianyingPro/User Data/Projects/com.lveditor.draft",'
                '"official_draft_root":"C:/Users/demo/AppData/Local/JianyingPro/User Data/Projects/com.lveditor.draft",'
                '"using_portable_draft_root":false}}'
            ),
            stderr="",
        )

        result = verify_release.verify_system_draft_resolution(Path("demo.exe"), Path("dist"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["resolved_mode"], "system")


if __name__ == "__main__":
    unittest.main()
