import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import shot_recall
import validate_shot_recall_reports


class ShotRecallValidationTests(unittest.TestCase):
    def test_build_recall_index_warns_and_falls_back_to_tfidf(self):
        model = Mock()
        model.encode.side_effect = RuntimeError("embedding failed")
        materials = [{"caption": "皮肤瘙痒抓挠", "tags": ["瘙痒"], "description": ""}]

        with patch.object(shot_recall, "_load_sentence_model", return_value=model), \
             patch("builtins.print") as mock_print:
            index = shot_recall.build_recall_index(materials)

        self.assertEqual(index["backend"], "tfidf")
        mock_print.assert_called()
        self.assertIn("回退 TF-IDF", mock_print.call_args[0][0])

    def test_load_jsonl_warns_and_skips_invalid_line(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "demo.jsonl"
            log_path.write_text('{"ok": 1}\n{bad json}\n{"ok": 2}\n', encoding="utf-8")

            with patch("builtins.print") as mock_print:
                rows = validate_shot_recall_reports._load_jsonl(str(log_path))

        self.assertEqual(rows, [{"ok": 1}, {"ok": 2}])
        mock_print.assert_called()
        self.assertIn("损坏的 JSONL 行", mock_print.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
