import json
import tempfile
import unittest
from pathlib import Path

import batch_otc_promo_workflow


class BatchOtcStateTests(unittest.TestCase):
    def test_load_batch_state_backs_up_corrupt_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "batch_run_state.json"
            state_path.write_text("{bad json", encoding="utf-8")

            loaded = batch_otc_promo_workflow._load_batch_state(str(state_path))

            self.assertEqual(loaded["results"], [])
            self.assertTrue(loaded["meta"]["recovered_from_corrupt_state"])
            backup_path = loaded["meta"]["backup_path"]
            self.assertTrue(backup_path)
            self.assertFalse(state_path.exists())
            self.assertTrue(Path(backup_path).exists())
            self.assertEqual(Path(backup_path).read_text(encoding="utf-8"), "{bad json")

    def test_load_batch_state_rejects_non_object_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "batch_run_state.json"
            state_path.write_text(json.dumps(["invalid"]), encoding="utf-8")

            loaded = batch_otc_promo_workflow._load_batch_state(str(state_path))

            self.assertEqual(loaded["results"], [])
            self.assertTrue(loaded["meta"]["recovered_from_corrupt_state"])


if __name__ == "__main__":
    unittest.main()
