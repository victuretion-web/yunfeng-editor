import importlib.util
import io
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


SCRIPTS_ROOT = Path(
    r"d:\trae\云锋剪辑\jianying-editor-skill-main\jianying-editor-skill-main\scripts"
)
PROJECT_BASE_PATH = SCRIPTS_ROOT / "core" / "project_base.py"


def _load_project_base_module():
    fake_pkg = types.ModuleType("pyJianYingDraft")
    fake_pkg.DraftFolder = object
    fake_pkg.__path__ = []
    module_name = "project_base_under_test"
    spec = importlib.util.spec_from_file_location(module_name, PROJECT_BASE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(module_name, None)
    sys.path.insert(0, str(SCRIPTS_ROOT))
    try:
        with patch.dict(sys.modules, {"pyJianYingDraft": fake_pkg}):
            spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


class ProjectBaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project_base = _load_project_base_module()

    def test_init_raises_clear_error_when_drafts_root_creation_fails(self):
        with (
            patch.object(self.project_base.os.path, "exists", return_value=False),
            patch.object(
                self.project_base.os,
                "makedirs",
                side_effect=OSError("disk full"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "创建剪映草稿根目录失败"):
                self.project_base.JyProjectBase("demo", drafts_root=r"D:\invalid\drafts")

    def test_try_release_project_lock_reports_sendkeys_failure(self):
        class FakeApp:
            def SendKeys(self, _keys):
                raise RuntimeError("uia failure")

        class FakeController:
            def __init__(self, keep_topmost=False):
                self.app = FakeApp()
                self.app_status = "pre_export"
                self.release_topmost_called = False

            def get_window(self, topmost=False):
                return None

            def release_topmost(self):
                self.release_topmost_called = True

        fake_pkg = types.ModuleType("pyJianYingDraft")
        fake_pkg.__path__ = []
        fake_submodule = types.ModuleType("pyJianYingDraft.jianying_controller")
        fake_submodule.JianyingController = FakeController

        stdout = io.StringIO()
        with patch.dict(
            sys.modules,
            {
                "pyJianYingDraft": fake_pkg,
                "pyJianYingDraft.jianying_controller": fake_submodule,
            },
        ):
            with redirect_stdout(stdout):
                released = self.project_base.JyProjectBase._try_release_project_lock(object())

        self.assertFalse(released)
        self.assertIn("failed to dismiss pre-export dialog", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
