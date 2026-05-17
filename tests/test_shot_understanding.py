import math
import os
import tempfile
import unittest
from unittest.mock import patch

import shot_understanding


class ShotUnderstandingTests(unittest.TestCase):
    def test_safe_video_fps_falls_back_for_nan(self):
        self.assertEqual(shot_understanding._safe_video_fps(float("nan")), 25.0)

    def test_safe_video_fps_respects_valid_value(self):
        self.assertEqual(shot_understanding._safe_video_fps(29.97), 29.97)
        self.assertEqual(shot_understanding._safe_video_fps("30"), 30.0)

    def test_resolve_opencv_readable_path_raises_when_copy_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            unicode_dir = os.path.join(temp_dir, "测试目录")
            os.makedirs(unicode_dir)
            cascade_path = os.path.join(unicode_dir, "haarcascade.xml")
            with open(cascade_path, "w", encoding="utf-8") as handle:
                handle.write("stub")

            with (
                patch.object(
                    shot_understanding.ctypes.windll.kernel32,
                    "GetShortPathNameW",
                    return_value=0,
                ),
                patch.object(
                    shot_understanding.shutil,
                    "copy2",
                    side_effect=OSError("disk full"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "复制 OpenCV 兼容路径失败"):
                    shot_understanding._resolve_opencv_readable_path(cascade_path)


if __name__ == "__main__":
    unittest.main()
