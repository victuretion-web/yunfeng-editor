import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import ui_main


class _Entry:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def delete(self, _start, _end=None):
        self.value = ""

    def insert(self, _index, value):
        self.value = value


class _Var:
    def __init__(self, value=False):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Tree:
    def __init__(self, children=None):
        self.children = list(children or [])

    def get_children(self):
        return list(self.children)

    def delete(self, item):
        if item in self.children:
            self.children.remove(item)


class _Button:
    def __init__(self):
        self.state = "normal"

    def config(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]


class _TextWidget:
    def __init__(self):
        self.content = ""
        self.state = "disabled"

    def configure(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]

    def delete(self, _start, _end):
        self.content = ""

    def insert(self, index, value):
        if index == "1.0":
            self.content = value
        else:
            self.content += value

    def see(self, _index):
        return None


class UiMainSemanticSettingsTests(unittest.TestCase):
    def test_default_provider_constants_are_siliconflow(self):
        self.assertEqual(ui_main.DEFAULT_LLM_BASE_URL, "https://api.siliconflow.cn")
        self.assertEqual(ui_main.DEFAULT_LLM_MODEL, "deepseek-ai/DeepSeek-V4-Flash")
        self.assertEqual(ui_main.DEFAULT_VLM_MODEL, "Qwen/Qwen3.6-35B-A3B")

    def test_main_ui_no_longer_adds_shot_library_tab(self):
        source = Path(ui_main.__file__).read_text(encoding="utf-8")
        self.assertNotIn('self.notebook.add(self.tab_shot_library, text="镜头库预览")', source)
        self.assertNotIn("self.build_shot_library_tab()", source)
        self.assertIn('text="实时运行日志"', source)

    def test_registry_repair_no_longer_prunes_current_task_whitelist(self):
        source = Path(ui_main.__file__).read_text(encoding="utf-8")
        self.assertNotIn("remove_stale_managed=bool(current_draft_names)", source)
        self.assertNotIn("草稿目录已收敛到当前任务", source)
        self.assertEqual(ui_main.TASK_UI_REFRESH_MS, 2500)

    def _build_dummy_ui(self):
        dummy = type("DummyUI", (), {})()
        dummy.var_main_shot_recall_enabled = _Var(True)
        dummy.var_main_shot_use_vlm = _Var(True)
        dummy.var_main_shot_force_refresh = _Var(True)
        dummy.var_main_strict_semantic = _Var(True)
        dummy.entry_main_shot_vlm_model = _Entry("qwen2.5-vl-7b-instruct")
        dummy.entry_main_shot_max_videos = _Entry("0")
        dummy.entry_main_shot_top_n = _Entry("24")
        dummy.entry_llm_key = _Entry("test-key")
        dummy.entry_llm_model = _Entry("deepseek-v3")
        dummy._get_selected_draft_mode = lambda: ui_main.DRAFT_MODE_AUTO
        dummy._resolve_selected_draft_root_info = lambda: {"resolved_root": r"D:\drafts"}
        dummy._resolve_worker_draft_root = lambda: r"D:\drafts"
        return dummy

    def test_apply_advanced_settings_env_includes_mainflow_semantic_flags(self):
        dummy = self._build_dummy_ui()

        env = ui_main.YunFengEditorUI._apply_advanced_settings_env(dummy, {})

        self.assertEqual(env["OTC_ENABLE_SHOT_RECALL"], "1")
        self.assertEqual(env["OTC_SHOT_RECALL_USE_VLM"], "1")
        self.assertEqual(env["OTC_SHOT_RECALL_FORCE_REFRESH"], "1")
        self.assertEqual(env["OTC_STRICT_SEMANTIC_INSERTION"], "1")
        self.assertEqual(env["OTC_DRAFT_MODE"], ui_main.DRAFT_MODE_AUTO)
        self.assertNotIn("OTC_DRAFT_ROOT", env)
        self.assertEqual(env["OTC_SHOT_RECALL_VLM_MODEL"], "qwen2.5-vl-7b-instruct")
        self.assertEqual(env["OTC_SHOT_RECALL_MAX_VIDEOS"], "0")
        self.assertEqual(env["OTC_SHOT_RECALL_TOP_N"], "24")

    def test_validate_mainflow_semantic_requirements_blocks_missing_vlm_model(self):
        dummy = self._build_dummy_ui()
        dummy.entry_main_shot_vlm_model = _Entry("")

        with patch.object(ui_main.messagebox, "showerror") as mock_error:
            allowed = ui_main.YunFengEditorUI._validate_mainflow_semantic_requirements(dummy)

        self.assertFalse(allowed)
        mock_error.assert_called_once()
        self.assertIn("视觉模型名称", mock_error.call_args.args[1])

    def test_clear_finished_tasks_removes_only_finished_rows(self):
        dummy = type("DummyUI", (), {})()
        dummy.running_tasks = {
            "Task-001": {"status": ui_main.STATUS_READY},
            "Task-002": {"status": ui_main.STATUS_FAILED},
            "Task-003": {"status": ui_main.STATUS_RUNNING},
        }
        dummy._tasks_lock = ui_main.threading.Lock()
        dummy.task_count = 3
        dummy.tree = _Tree(["Task-001", "Task-002", "Task-003"])
        dummy._task_hit_all_rows = [{"a": 1}]
        dummy._task_hit_rows = {"x": 1}
        dummy._snapshot_running_tasks = lambda: dict(dummy.running_tasks)
        dummy._sync_task_counter_from_running_tasks = lambda: ui_main.YunFengEditorUI._sync_task_counter_from_running_tasks(dummy)
        dummy._write_task_report_snapshot = lambda: None
        dummy._clear_task_hit_preview = lambda _text: None
        dummy.on_task_tree_selected = lambda: None

        with patch.object(ui_main.messagebox, "showinfo") as mock_info:
            ui_main.YunFengEditorUI.clear_finished_tasks(dummy)

        self.assertEqual(set(dummy.running_tasks.keys()), {"Task-003"})
        self.assertEqual(dummy.task_count, 3)
        self.assertEqual(dummy.tree.get_children(), ["Task-003"])
        self.assertEqual(dummy._task_hit_all_rows, [])
        self.assertEqual(dummy._task_hit_rows, {})
        mock_info.assert_called_once()

    def test_test_vision_connectivity_uses_vlm_model(self):
        dummy = self._build_dummy_ui()
        dummy.entry_main_shot_vlm_model = _Entry("doubao-seed-1.6-vision-250815")
        dummy.entry_llm_base_url = _Entry("https://api.kuai.host")
        dummy._get_llm_base_url = lambda: "https://api.kuai.host"
        dummy._save_ui_settings = lambda: None
        dummy.update_idletasks = lambda: None
        dummy.btn_test_vision = _Button()

        with patch("llm_clip_matcher.test_vision_connectivity", return_value=(True, "ok")) as mock_test, \
             patch.object(ui_main.messagebox, "showinfo") as mock_info:
            ui_main.YunFengEditorUI.test_vision_connectivity(dummy)

        mock_test.assert_called_once_with(
            api_key="test-key",
            model="doubao-seed-1.6-vision-250815",
            base_url="https://api.kuai.host",
        )
        mock_info.assert_called_once()
        self.assertEqual(dummy.btn_test_vision.state, "normal")

    def test_sync_task_draft_to_official_root_retries_official_sync_after_portable_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            portable_root = Path(temp_dir) / "portable"
            official_root = Path(temp_dir) / "official"
            draft_dir = portable_root / "OTC推广_demo"
            draft_dir.mkdir(parents=True)
            official_root.mkdir(parents=True)

            dummy = type("DummyUI", (), {})()
            dummy.output_dir = temp_dir
            dummy._resolve_selected_draft_root_info = lambda: {
                "requested_mode": ui_main.DRAFT_MODE_AUTO,
                "resolved_root": str(portable_root),
                "fallback_reason": "系统剪映草稿目录不可写，已自动切换到便携目录",
            }
            dummy._build_portable_draft_status_note = lambda _info: "portable"
            dummy._log_nonfatal_issue = lambda *args, **kwargs: None
            dummy._short_path = lambda value, max_len=88: value

            def fake_sync_managed_drafts(**kwargs):
                target_path = Path(kwargs["target_root"]) / Path(kwargs["include_names"][0])
                target_path.mkdir(parents=True, exist_ok=True)
                return {"copied": [kwargs["include_names"][0]]}

            with patch.object(ui_main, "get_official_draft_root", return_value=str(official_root)), \
                 patch.object(ui_main, "sync_managed_drafts", side_effect=fake_sync_managed_drafts) as mock_sync:
                result = ui_main.YunFengEditorUI._sync_task_draft_to_official_root(dummy, str(draft_dir))

        self.assertEqual(result["draft_dir"], str(official_root / "OTC推广_demo"))
        self.assertEqual(result["status_note"], "")
        mock_sync.assert_called_once()

    def test_runtime_log_refresh_appends_delta_instead_of_full_rewrite(self):
        dummy = type("DummyUI", (), {})()
        dummy.txt_task_runtime_log = _TextWidget()
        dummy._last_task_runtime_log_text = ""

        ui_main.YunFengEditorUI._set_task_runtime_log_text(dummy, "第一行")
        self.assertEqual(dummy.txt_task_runtime_log.content, "第一行")

        ui_main.YunFengEditorUI._set_task_runtime_log_text(dummy, "第一行\n第二行")
        self.assertEqual(dummy.txt_task_runtime_log.content, "第一行\n第二行")
        self.assertEqual(dummy.txt_task_runtime_log.state, "disabled")

    def test_portable_draft_mode_prompts_for_confirmation_before_submit(self):
        dummy = type("DummyUI", (), {})()
        dummy._latest_preflight_report = {
            "checks": {
                "using_portable_draft_root": True,
                "draft_root": r"D:\portable\drafts",
                "detected_jianying_version": "5.9.0.11632",
            }
        }

        with patch.object(ui_main.messagebox, "askyesno", return_value=False) as mock_confirm:
            allowed = ui_main.YunFengEditorUI._warn_portable_draft_fallback_before_submit(dummy)

        self.assertFalse(allowed)
        mock_confirm.assert_called_once()
        self.assertIn("将使用便携草稿目录继续生成", mock_confirm.call_args.args[1])

    def test_portable_draft_guard_allows_worker_verified_system_mode(self):
        dummy = type("DummyUI", (), {})()
        dummy._latest_preflight_report = {
            "checks": {
                "using_portable_draft_root": False,
                "draft_root": r"C:\Users\demo\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft",
            }
        }

        self.assertTrue(ui_main.YunFengEditorUI._warn_portable_draft_fallback_before_submit(dummy))


if __name__ == "__main__":
    unittest.main()
