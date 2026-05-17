import tempfile
import unittest
from unittest.mock import Mock, patch

import bgm_pipeline


class BgmPipelineTests(unittest.TestCase):
    def test_normalize_loudness_returns_false_when_stderr_contains_error(self):
        mock_result = Mock(returncode=0, stderr="Error while filtering")
        with patch.object(bgm_pipeline, "run_hidden", return_value=mock_result), \
             patch.object(bgm_pipeline.os.path, "exists", return_value=True):
            ok, message = bgm_pipeline._normalize_loudness("in.wav", "out.wav", target_lufs=-16)
        self.assertFalse(ok)
        self.assertIn("loudnorm", message)

    def test_prepare_bgm_for_timeline_wraps_decode_error(self):
        with tempfile.TemporaryDirectory() as output_dir, \
             patch.object(bgm_pipeline.AudioSegment, "from_file", side_effect=OSError("bad audio")):
            with self.assertRaises(RuntimeError) as ctx:
                bgm_pipeline.prepare_bgm_for_timeline(
                    bgm_path="broken.mp3",
                    target_duration_sec=10.0,
                    output_dir=output_dir,
                    prefix="demo",
                )
        self.assertIn("背景音乐读取失败", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
