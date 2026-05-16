import unittest

from insert_density import (
    analyze_insert_density,
    auto_fill_density_gaps,
    get_density_template_config,
)


class InsertDensityTests(unittest.TestCase):
    def test_get_density_template_config_returns_copy(self):
        config = get_density_template_config("快节奏")
        config["window_seconds"] = 99
        fresh = get_density_template_config("快节奏")
        self.assertEqual(fresh["window_seconds"], 30.0)

    def test_analyze_insert_density_returns_empty_for_invalid_duration(self):
        self.assertEqual(analyze_insert_density([], video_duration=0), [])

    def test_analyze_insert_density_marks_missing_window(self):
        windows = analyze_insert_density(
            matches=[
                {"start_time": 0.0, "end_time": 2.0},
                {"start_time": 6.0, "end_time": 8.0},
            ],
            video_duration=20.0,
            window_seconds=10.0,
            min_segments_per_window=2,
        )
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]["status"], "达标")
        self.assertEqual(windows[1]["status"], "待补齐")

    def test_auto_fill_density_gaps_adds_segments_for_sparse_window(self):
        merged, windows, fill_actions = auto_fill_density_gaps(
            candidate_matches=[],
            video_duration=20.0,
            window_seconds=10.0,
            min_segments_per_window=1,
            insert_min_duration=2.0,
            insert_max_duration=3.0,
            target_ratio=0.0,
        )
        self.assertGreaterEqual(len(fill_actions), 2)
        self.assertEqual(len(merged), len(fill_actions))
        self.assertTrue(all(item["is_density_fill"] for item in merged))
        self.assertTrue(all(window["status"] == "达标" for window in windows))

    def test_auto_fill_density_gaps_improves_total_coverage_ratio(self):
        merged, _, fill_actions = auto_fill_density_gaps(
            candidate_matches=[{"start_time": 0.0, "end_time": 2.0, "duration": 2.0, "semantic_type": "product"}],
            video_duration=20.0,
            window_seconds=10.0,
            min_segments_per_window=1,
            insert_min_duration=2.0,
            insert_max_duration=3.0,
            target_ratio=0.4,
        )
        total_duration = sum(item["duration"] for item in merged)
        self.assertGreaterEqual(total_duration / 20.0, 0.4)
        self.assertGreaterEqual(len(fill_actions), 1)


if __name__ == "__main__":
    unittest.main()
