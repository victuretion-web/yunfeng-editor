import unittest

from semantic_matcher import (
    SEMANTIC_ANCHORS,
    analyze_broll_ratio,
    build_semantic_profile,
    infer_intent,
    infer_semantic_type,
    rank_materials_by_semantics,
)


class SemanticMatcherTests(unittest.TestCase):
    def test_infer_semantic_type_prefers_symptom_keywords(self):
        text = "夜间皮肤瘙痒反复发作，影响睡眠"
        self.assertEqual(infer_semantic_type(text), "symptom")

    def test_infer_semantic_type_prefers_product_keywords(self):
        text = "乳膏抑菌止痒，涂抹使用方便"
        self.assertEqual(infer_semantic_type(text), "product")

    def test_infer_intent_defaults_to_product_usage(self):
        text = "这款乳膏建议清洗后均匀涂抹"
        self.assertEqual(infer_intent(text, "product"), "product_usage")

    def test_build_semantic_profile_adds_anchor_and_entities(self):
        profile = build_semantic_profile(
            text="夜间皮肤瘙痒抓挠特写",
            semantic_type="symptom",
            tags=["瘙痒", "夜间"],
        )
        self.assertEqual(profile["semantic_type"], "symptom")
        self.assertIn("瘙痒", profile["entities"])
        self.assertIn(SEMANTIC_ANCHORS["symptom"], profile["token_weights"])
        self.assertEqual(profile["intent"], "symptom_feeling")

    def test_rank_materials_by_semantics_prefers_matching_material(self):
        candidate = {
            "text": "药膏均匀涂抹使用演示",
            "semantic_type": "product",
            "tags": ["乳膏", "涂抹"],
            "intent": "product_usage",
            "action_type": "apply_ointment",
            "scene_hint": "bathroom_usage",
            "entities": ["乳膏", "涂抹"],
        }
        product_material = {
            "filename": "药膏涂抹展示.mp4",
            "semantic_type": "product",
            "tags": ["乳膏", "涂抹", "使用"],
            "visual_quality": 0.92,
        }
        symptom_material = {
            "filename": "夜间抓挠困扰.mp4",
            "semantic_type": "symptom",
            "tags": ["瘙痒", "抓挠"],
            "visual_quality": 0.92,
        }

        ranked = rank_materials_by_semantics(candidate, [symptom_material, product_material])
        self.assertEqual(ranked[0]["material"]["filename"], "药膏涂抹展示.mp4")
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])

    def test_analyze_broll_ratio_marks_unfilled_window(self):
        report = analyze_broll_ratio(
            matches=[
                {"start_time": 0.0, "end_time": 5.0, "duration": 5.0},
                {"start_time": 10.0, "end_time": 15.0, "duration": 5.0},
            ],
            video_duration=40.0,
            target_ratio=0.5,
            window_seconds=20,
        )
        self.assertEqual(report["total_status"], "待补齐")
        self.assertEqual(len(report["windows"]), 2)
        self.assertEqual(report["windows"][0]["status"], "达标")
        self.assertEqual(report["windows"][1]["status"], "待补齐")


if __name__ == "__main__":
    unittest.main()
