import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import draft_registry


class DraftRegistryTests(unittest.TestCase):
    @staticmethod
    def _write_valid_draft(draft_dir: Path, draft_id: str = "draft-1") -> None:
        draft_dir.mkdir(parents=True, exist_ok=True)
        (draft_dir / "draft_content.json").write_text(
            '{"id": "' + draft_id + '"}',
            encoding="utf-8",
        )
        (draft_dir / "draft_meta_info.json").write_text(
            '{"draft_id": "' + draft_id + '"}',
            encoding="utf-8",
        )

    def test_normalize_draft_mode_uses_explicit_value(self):
        self.assertEqual(draft_registry.normalize_draft_mode("SYSTEM"), draft_registry.DRAFT_MODE_SYSTEM)

    def test_normalize_draft_mode_falls_back_to_auto_for_invalid_value(self):
        with patch.dict(os.environ, {"OTC_DRAFT_MODE": "unknown"}, clear=False):
            self.assertEqual(draft_registry.normalize_draft_mode(), draft_registry.DRAFT_MODE_AUTO)

    def test_is_portable_draft_root_matches_runtime_path(self):
        with patch.object(draft_registry, "get_portable_draft_root", return_value=r"D:\portable\draft"):
            self.assertTrue(draft_registry.is_portable_draft_root(r"D:\portable\draft"))
            self.assertFalse(draft_registry.is_portable_draft_root(r"D:\other\draft"))

    def test_get_draft_root_info_prefers_system_when_writable(self):
        with patch.object(draft_registry, "get_official_draft_root", return_value=r"D:\official\draft"), \
             patch.object(draft_registry, "get_portable_draft_root", return_value=r"D:\portable\draft"), \
             patch.object(draft_registry, "_ensure_writable_directory", side_effect=[True, True]), \
             patch.object(draft_registry.os, "makedirs"):
            info = draft_registry.get_draft_root_info(draft_registry.DRAFT_MODE_AUTO)
        self.assertEqual(info["resolved_mode"], draft_registry.DRAFT_MODE_SYSTEM)
        self.assertEqual(info["resolved_root"], r"D:\official\draft")
        self.assertFalse(info["fallback_reason"])

    def test_get_draft_root_info_keeps_system_root_when_probe_fails(self):
        with patch.object(draft_registry, "get_official_draft_root", return_value=r"D:\official\draft"), \
             patch.object(draft_registry, "get_portable_draft_root", return_value=r"D:\portable\draft"), \
             patch.object(draft_registry, "_ensure_writable_directory", side_effect=[False, True]), \
             patch.object(draft_registry.os, "makedirs"):
            info = draft_registry.get_draft_root_info(draft_registry.DRAFT_MODE_SYSTEM)
        self.assertEqual(info["resolved_mode"], draft_registry.DRAFT_MODE_SYSTEM)
        self.assertEqual(info["resolved_root"], r"D:\official\draft")
        self.assertFalse(info["fallback_reason"])

    def test_get_draft_root_info_auto_prefers_system_root_when_available(self):
        with patch.object(draft_registry, "get_official_draft_root", return_value=r"D:\official\draft"), \
             patch.object(draft_registry, "get_portable_draft_root", return_value=r"D:\portable\draft"), \
             patch.object(draft_registry, "_ensure_writable_directory", side_effect=[False, True]), \
             patch.object(draft_registry.os, "makedirs"):
            info = draft_registry.get_draft_root_info(draft_registry.DRAFT_MODE_AUTO)
        self.assertEqual(info["resolved_mode"], draft_registry.DRAFT_MODE_SYSTEM)
        self.assertEqual(info["resolved_root"], r"D:\official\draft")
        self.assertFalse(info["fallback_reason"])

    def test_get_draft_root_ignores_override_when_not_in_portable_mode(self):
        with patch.dict(
            os.environ,
            {
                "OTC_DRAFT_MODE": draft_registry.DRAFT_MODE_AUTO,
                "OTC_DRAFT_ROOT": r"D:\portable\draft",
            },
            clear=False,
        ), patch.object(draft_registry, "get_draft_root_info", return_value={"resolved_root": r"C:\official\draft"}) as mock_info:
            resolved = draft_registry.get_draft_root()
        self.assertEqual(resolved, r"C:\official\draft")
        mock_info.assert_called_once_with(draft_registry.DRAFT_MODE_AUTO)

    def test_get_draft_root_honors_override_in_portable_mode(self):
        with patch.dict(
            os.environ,
            {
                "OTC_DRAFT_MODE": draft_registry.DRAFT_MODE_PORTABLE,
                "OTC_DRAFT_ROOT": r"D:\portable\draft",
            },
            clear=False,
        ), patch.object(draft_registry, "get_draft_root_info") as mock_info:
            resolved = draft_registry.get_draft_root()
        self.assertEqual(resolved, r"D:\portable\draft")
        mock_info.assert_not_called()

    def test_read_lock_payload_handles_invalid_content(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="ascii") as handle:
            handle.write("bad payload")
            lock_path = handle.name
        try:
            pid, created_at = draft_registry._read_lock_payload(lock_path)
        finally:
            os.remove(lock_path)
        self.assertIsNone(pid)
        self.assertIsNone(created_at)

    def test_pid_is_running_rejects_empty_values(self):
        self.assertFalse(draft_registry._pid_is_running(None))
        self.assertFalse(draft_registry._pid_is_running(0))

    def test_commit_staged_draft_restores_backup_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as root:
            target_path = Path(root) / "target"
            staged_path = Path(root) / "staged"
            target_path.mkdir()
            staged_path.mkdir()
            (target_path / "marker.txt").write_text("original", encoding="utf-8")
            (staged_path / "marker.txt").write_text("staged", encoding="utf-8")

            original_replace = draft_registry.os.replace
            call_count = {"value": 0}

            def flaky_replace(src, dst):
                call_count["value"] += 1
                if call_count["value"] == 2:
                    raise PermissionError("simulated copy failure")
                return original_replace(src, dst)

            with patch.object(draft_registry.os, "replace", side_effect=flaky_replace):
                with self.assertRaises(PermissionError):
                    draft_registry._commit_staged_draft(str(staged_path), str(target_path))

            self.assertTrue(target_path.is_dir())
            self.assertEqual((target_path / "marker.txt").read_text(encoding="utf-8"), "original")
            self.assertFalse(staged_path.exists())

    def test_merge_latest_root_meta_entries_preserves_external_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            draft_root = Path(temp_dir)
            draft_dir = draft_root / "OTC推广_demo"
            self._write_valid_draft(draft_dir, draft_id="external-1")
            latest_root_meta = {
                "all_draft_store": [
                    {
                        "draft_fold_path": str(draft_dir).replace("\\", "/"),
                        "draft_id": "external-1",
                        "draft_json_file": str(draft_dir / "draft_content.json").replace("\\", "/"),
                    }
                ],
                "extra_flag": True,
            }

            merged_entries, preserved_names = draft_registry._merge_latest_root_meta_entries(
                str(draft_root),
                [],
                latest_root_meta,
            )
            payload = draft_registry._build_root_meta_payload(
                str(draft_root),
                merged_entries,
                latest_root_meta,
            )

            self.assertEqual(preserved_names, ["OTC推广_demo"])
            self.assertEqual(len(merged_entries), 1)
            self.assertEqual(merged_entries[0]["draft_id"], "external-1")
            self.assertTrue(payload["extra_flag"])
            self.assertEqual(payload["draft_ids"], 1)

    def test_sync_temp_and_backup_names_are_not_hidden(self):
        temp_name = draft_registry._build_temp_dir_name("OTC推广_demo")
        backup_name = draft_registry._build_backup_dir_name("OTC推广_demo")
        self.assertFalse(temp_name.startswith("."))
        self.assertFalse(backup_name.startswith("."))
        self.assertIn("sync_tmp_", temp_name)
        self.assertIn("sync_bak_", backup_name)

    def test_get_sync_staging_root_prefers_same_drive_temp_directory(self):
        with patch.object(draft_registry.tempfile, "gettempdir", return_value=r"C:\Temp"):
            staging_root = draft_registry._get_sync_staging_root(
                r"C:\Users\Administrator\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft"
            )
        self.assertEqual(staging_root, r"C:\Temp\yunfeng_draft_sync")

    def test_get_sync_staging_root_falls_back_to_target_root_when_drive_differs(self):
        with patch.object(draft_registry.tempfile, "gettempdir", return_value=r"D:\Temp"):
            staging_root = draft_registry._get_sync_staging_root(r"C:\Jianying\draft")
        self.assertEqual(staging_root, r"C:\Jianying\draft")

    def test_writable_probe_file_is_not_hidden(self):
        source = Path(draft_registry.__file__).read_text(encoding="utf-8")
        self.assertIn('"write_probe.tmp"', source)
        self.assertNotIn('".write_probe.tmp"', source)


if __name__ == "__main__":
    unittest.main()
