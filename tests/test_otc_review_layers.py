import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = PROJECT_ROOT / "otc_promo_workflow.py"


class OtcReviewLayerSourceTests(unittest.TestCase):
    def test_review_only_overlay_tracks_stay_under_review_flag(self):
        """Watermark track should still be under is_review_version guard,
        but ad_review and sticker tracks should be unconditional."""
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

        # Watermark track should still be guarded by is_review_version
        watermark_nodes = []
        for node in ast.walk(target_function):
            if isinstance(node, ast.Constant) and node.value in {"07_Review_Watermark"}:
                watermark_nodes.append(node)

        self.assertTrue(watermark_nodes)
        for node in watermark_nodes:
            self.assertTrue(
                any(start <= node.lineno <= end for start, end in guarded_ranges),
                msg=f"{node.value} should stay inside the is_review_version guard",
            )

    def test_ad_review_and_sticker_tracks_are_unconditional(self):
        """Ad review and sticker tracks should NOT be under is_review_version guard."""
        source = WORKFLOW_PATH.read_text(encoding="utf-8")
        module = ast.parse(source)
        target_function = next(
            node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "create_otc_promo_video"
        )

        guarded_ranges = []
        for node in ast.walk(target_function):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "is_review_version":
                guarded_ranges.append((node.lineno, getattr(node, "end_lineno", node.lineno)))

        review_track_nodes = []
        for node in ast.walk(target_function):
            if isinstance(node, ast.Constant) and node.value in {"05_Ad_Review", "06_Top_Sticker"}:
                review_track_nodes.append(node)

        self.assertTrue(review_track_nodes)
        for node in review_track_nodes:
            self.assertFalse(
                any(start <= node.lineno <= end for start, end in guarded_ranges),
                msg=f"{node.value} should NOT be inside the is_review_version guard (should be unconditional)",
            )


if __name__ == "__main__":
    unittest.main()
