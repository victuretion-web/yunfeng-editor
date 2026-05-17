import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = PROJECT_ROOT / "otc_promo_workflow.py"


class OtcReviewLayerSourceTests(unittest.TestCase):
    def test_single_output_keeps_only_watermark_under_review_flag(self):
        """Single-output flow should still keep the review watermark behind the review flag."""
        source = WORKFLOW_PATH.read_text(encoding="utf-8")
        module = ast.parse(source)
        target_function = next(
            node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "create_otc_promo_video"
        )

        guarded_ranges = []
        for node in ast.walk(target_function):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "is_review_version":
                guarded_ranges.append((node.lineno, getattr(node, "end_lineno", node.lineno)))

        self.assertTrue(guarded_ranges)

        guarded_blocks = [
            ast.get_source_segment(source, node) or ""
            for node in ast.walk(target_function)
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "is_review_version"
        ]

        self.assertTrue(guarded_blocks)
        combined = "\n".join(guarded_blocks)
        self.assertIn("07_Review_Watermark", combined)
        self.assertIn('draft.TrackType.text, "07_Review_Watermark"', combined)
        self.assertNotIn("05_Ad_Review", combined)
        self.assertNotIn("06_Top_Sticker", combined)

        self.assertIn('project.script.add_track(draft.TrackType.video, "05_Ad_Review"', source)
        self.assertIn('project.script.add_track(draft.TrackType.video, "06_Top_Sticker"', source)

    def test_main_flow_exports_single_stable_draft(self):
        """The worker entry should output one stable draft instead of dual variants."""
        source = WORKFLOW_PATH.read_text(encoding="utf-8")
        module = ast.parse(source)
        main_function = next(
            node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "main"
        )

        helper_calls = []
        for node in ast.walk(main_function):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                helper_calls.append(node.func.id)

        self.assertIn("create_otc_promo_video", helper_calls)
        self.assertNotIn("create_otc_promo_video_variants", helper_calls)


if __name__ == "__main__":
    unittest.main()
