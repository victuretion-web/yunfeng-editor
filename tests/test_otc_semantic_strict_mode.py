import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = PROJECT_ROOT / "otc_promo_workflow.py"


class OtcSemanticStrictModeTests(unittest.TestCase):
    def test_product_thresholds_are_tighter_than_before(self):
        source = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn('intent == "product_effect"', source)
        self.assertIn("0.68", source)
        self.assertIn('intent == "product_usage"', source)
        self.assertIn("0.64", source)

    def test_classify_match_quality_labels(self):
        source = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn('return "strong", "强语义命中"', source)
        self.assertIn('return "relaxed", "放宽阈值命中"', source)
        self.assertIn('return "degraded", "泛素材降级"', source)

    def test_single_output_flow_no_longer_declares_variant_helper(self):
        source = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("def create_otc_promo_video_variants", source)

    def test_material_pool_warning_reports_usable_count(self):
        source = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("当前可用素材不足", source)
        self.assertIn('"usable_count"', source)
        self.assertIn('"preferred_count"', source)

    def test_preflight_warns_when_portable_draft_mode_is_used(self):
        source = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn('report["warnings"].append(portable_message)', source)
        self.assertIn("生成完成后可手动同步到剪映草稿目录", source)

    def test_preflight_writable_probe_is_not_hidden(self):
        source = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn('"draft_write_probe.tmp"', source)
        self.assertNotIn('".draft_write_probe.tmp"', source)


if __name__ == "__main__":
    unittest.main()
