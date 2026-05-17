import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch


CONFIG_MODULE_PATH = Path(r"d:\trae\云锋剪辑\FireRed-OpenStoryline-main\src\open_storyline\config.py")


def _load_open_storyline_config_module():
    module_name = f"open_storyline_config_test_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, CONFIG_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class OpenStorylineConfigTests(unittest.TestCase):
    def test_default_config_path_prefers_local_file(self):
        if not CONFIG_MODULE_PATH.exists():
            self.skipTest("OpenStoryline 子项目未检出，跳过本地配置路径测试")
        config_module = _load_open_storyline_config_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            previous_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
                Path("config.local.toml").write_text("", encoding="utf-8")
                with patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(config_module.default_config_path(), "config.local.toml")
            finally:
                os.chdir(previous_cwd)

    def test_default_config_path_respects_explicit_env_override(self):
        if not CONFIG_MODULE_PATH.exists():
            self.skipTest("OpenStoryline 子项目未检出，跳过环境变量覆盖测试")
        config_module = _load_open_storyline_config_module()
        with patch.dict(os.environ, {"OPENSTORYLINE_CONFIG": "custom.toml"}, clear=True):
            self.assertEqual(config_module.default_config_path(), "custom.toml")


if __name__ == "__main__":
    unittest.main()
