import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ui_main


class UiDraftFallbackTests(unittest.TestCase):
    def _build_dummy_ui(self, draft_info):
        dummy = type("DummyUI", (), {})()
        dummy._resolve_selected_draft_root_info = lambda: dict(draft_info)
        dummy._short_path = lambda path, max_len=72: str(path)
        dummy._build_portable_draft_status_note = lambda info: ui_main.YunFengEditorUI._build_portable_draft_status_note(dummy, info)
        dummy._log_nonfatal_issue = lambda *args, **kwargs: None
        dummy._is_directory_writable = lambda path: False
        dummy.output_dir = r"D:\temp\output"
        return dummy

    def test_sync_task_draft_skips_sync_in_explicit_portable_mode(self):
        draft_info = {
            "requested_mode": ui_main.DRAFT_MODE_PORTABLE,
            "resolved_mode": ui_main.DRAFT_MODE_PORTABLE,
            "resolved_root": r"D:\portable\drafts",
            "official_writable": False,
            "fallback_reason": "",
        }
        dummy = self._build_dummy_ui(draft_info)

        with tempfile.TemporaryDirectory() as temp_dir:
            draft_dir = Path(temp_dir) / "OTC推广_demo"
            draft_dir.mkdir()
            with patch.object(ui_main, "sync_managed_drafts") as mock_sync:
                result = ui_main.YunFengEditorUI._sync_task_draft_to_official_root(dummy, str(draft_dir))

        mock_sync.assert_not_called()
        self.assertEqual(result["draft_dir"], str(draft_dir))
        self.assertIn("便携目录", result["status_note"])
        self.assertIn("不会直接出现在剪映首页", result["status_note"])

    def test_sync_task_draft_attempts_system_sync_when_auto_reports_portable_root(self):
        draft_info = {
            "requested_mode": ui_main.DRAFT_MODE_AUTO,
            "resolved_mode": ui_main.DRAFT_MODE_PORTABLE,
            "resolved_root": r"D:\portable\drafts",
            "official_writable": False,
            "fallback_reason": "系统剪映草稿目录不可写，已自动切换到便携目录",
        }
        dummy = self._build_dummy_ui(draft_info)

        with tempfile.TemporaryDirectory() as temp_dir:
            draft_dir = Path(temp_dir) / "OTC推广_demo"
            draft_dir.mkdir()
            official_root = Path(temp_dir) / "official"
            official_root.mkdir()

            with patch.object(ui_main, "get_official_draft_root", return_value=str(official_root)), \
                 patch.object(ui_main, "sync_managed_drafts") as mock_sync:
                result = ui_main.YunFengEditorUI._sync_task_draft_to_official_root(dummy, str(draft_dir))

        mock_sync.assert_called_once()
        self.assertEqual(result["draft_dir"], str(draft_dir))
        self.assertIn("系统剪映目录未看到同步结果", result["status_note"])


if __name__ == "__main__":
    unittest.main()
