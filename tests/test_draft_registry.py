import os
import tempfile
import unittest
from unittest.mock import patch

import draft_registry


class DraftRegistryTests(unittest.TestCase):
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

    def test_get_draft_root_info_falls_back_to_portable_when_system_unwritable(self):
        with patch.object(draft_registry, "get_official_draft_root", return_value=r"D:\official\draft"), \
             patch.object(draft_registry, "get_portable_draft_root", return_value=r"D:\portable\draft"), \
             patch.object(draft_registry, "_ensure_writable_directory", side_effect=[False, True]), \
             patch.object(draft_registry.os, "makedirs"):
            info = draft_registry.get_draft_root_info(draft_registry.DRAFT_MODE_SYSTEM)
        self.assertEqual(info["resolved_mode"], draft_registry.DRAFT_MODE_PORTABLE)
        self.assertEqual(info["resolved_root"], r"D:\portable\draft")
        self.assertIn("已自动切换到便携目录", info["fallback_reason"])

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


if __name__ == "__main__":
    unittest.main()
