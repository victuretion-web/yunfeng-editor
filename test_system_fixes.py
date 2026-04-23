import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from batch_runtime_config import BATCH_CONCURRENCY
from draft_registry import file_lock, reconcile_root_meta
from material_pool_rules import are_materials_similar, validate_material_pools
from llm_clip_matcher import test_llm_connectivity
from media_file_rules import scan_video_file_paths, validate_speech_video_file
from otc_promo_workflow import UsageTracker, collect_video_files, smart_material_matching
from text_output_utils import decode_process_output, repair_mojibake_text
from ui_main import YunFengEditorUI
from timeline_utils import (
    layout_segments_on_tracks,
    sanitize_non_overlapping_segments,
    seconds_to_microseconds,
)


class TimelineUtilsTests(unittest.TestCase):
    def test_sanitize_non_overlapping_segments_shifts_and_drops(self):
        segments = [
            {"start_time": 0.0, "end_time": 2.0, "duration": 2.0},
            {"start_time": 1.5, "end_time": 3.0, "duration": 1.5},
            {"start_time": 3.0, "end_time": 3.1, "duration": 0.1},
        ]

        cleaned, stats = sanitize_non_overlapping_segments(segments, total_duration=10.0, min_duration=0.35)

        self.assertEqual(len(cleaned), 2)
        self.assertGreaterEqual(stats["shifted_count"], 1)
        self.assertEqual(stats["dropped_count"], 1)
        self.assertGreaterEqual(cleaned[1]["start_time"], cleaned[0]["end_time"])
        self.assertGreaterEqual(cleaned[1]["start_us"], cleaned[0]["end_us"])

    def test_sanitize_non_overlapping_segments_avoids_microsecond_overlap(self):
        segments = [
            {"start_time": 14.056, "end_time": 16.592, "duration": 2.536},
            {"start_time": 16.592, "end_time": 19.128, "duration": 2.536},
        ]

        cleaned, stats = sanitize_non_overlapping_segments(segments, total_duration=30.0)

        self.assertEqual(len(cleaned), 2)
        self.assertEqual(stats["shifted_count"], 0)
        self.assertEqual(cleaned[0]["end_us"], cleaned[1]["start_us"])

    def test_layout_segments_on_tracks_avoids_microsecond_overlap(self):
        segments = [
            {"text": "第一句", "start": 14.056, "end": 16.592},
            {"text": "第二句", "start": 16.592, "end": 19.128},
        ]

        laid_out, stats = layout_segments_on_tracks(segments, total_duration=30.0)

        self.assertEqual(len(laid_out), 2)
        self.assertEqual(stats["track_count"], 1)
        self.assertEqual(laid_out[0]["end_us"], laid_out[1]["start_us"])
        self.assertEqual(laid_out[0]["end_us"], seconds_to_microseconds(16.592))


class DraftRegistryTests(unittest.TestCase):
    def test_reconcile_root_meta_rebuilds_index_from_disk(self):
        with tempfile.TemporaryDirectory() as root:
            draft_name = "OTC推广_测试草稿"
            draft_dir = os.path.join(root, draft_name)
            os.makedirs(draft_dir, exist_ok=True)

            with open(os.path.join(draft_dir, "draft_content.json"), "w", encoding="utf-8") as f:
                json.dump({"id": "CONTENT-ID"}, f, ensure_ascii=False)
            with open(os.path.join(draft_dir, "draft_meta_info.json"), "w", encoding="utf-8") as f:
                json.dump({"draft_id": "META-ID", "tm_duration": 5000000}, f, ensure_ascii=False)

            report = reconcile_root_meta(draft_root=root, restore_project_drafts=False)
            self.assertIn(draft_name, report["registered_drafts"])

            with open(os.path.join(root, "root_meta_info.json"), "r", encoding="utf-8") as f:
                root_meta = json.load(f)

            self.assertEqual(len(root_meta["all_draft_store"]), 1)
            self.assertEqual(root_meta["all_draft_store"][0]["draft_id"], "META-ID")

    def test_reconcile_root_meta_restores_from_recycle_bin(self):
        with tempfile.TemporaryDirectory() as root:
            recycle_bin = os.path.join(root, ".recycle_bin")
            os.makedirs(recycle_bin, exist_ok=True)

            draft_name = "OTC推广_回收站草稿"
            recycle_draft_dir = os.path.join(recycle_bin, draft_name)
            os.makedirs(recycle_draft_dir, exist_ok=True)

            with open(os.path.join(recycle_draft_dir, "draft_content.json"), "w", encoding="utf-8") as f:
                json.dump({"id": "RECYCLE-CONTENT"}, f, ensure_ascii=False)
            with open(os.path.join(recycle_draft_dir, "draft_meta_info.json"), "w", encoding="utf-8") as f:
                json.dump({"draft_id": "RECYCLE-ID", "tm_duration": 6000000}, f, ensure_ascii=False)

            report = reconcile_root_meta(draft_root=root, restore_project_drafts=True)
            self.assertIn(draft_name, report["restored_from_recycle"])
            self.assertTrue(os.path.isdir(os.path.join(root, draft_name)))

    def test_reconcile_root_meta_does_not_restore_recycle_bin_by_default(self):
        with tempfile.TemporaryDirectory() as root:
            recycle_bin = os.path.join(root, ".recycle_bin")
            os.makedirs(recycle_bin, exist_ok=True)

            draft_name = "OTC推广_已删除草稿"
            recycle_draft_dir = os.path.join(recycle_bin, draft_name)
            os.makedirs(recycle_draft_dir, exist_ok=True)

            with open(os.path.join(recycle_draft_dir, "draft_content.json"), "w", encoding="utf-8") as f:
                json.dump({"id": "RECYCLE-CONTENT"}, f, ensure_ascii=False)
            with open(os.path.join(recycle_draft_dir, "draft_meta_info.json"), "w", encoding="utf-8") as f:
                json.dump({"draft_id": "RECYCLE-ID", "tm_duration": 6000000}, f, ensure_ascii=False)

            report = reconcile_root_meta(draft_root=root, restore_project_drafts=False)

            self.assertEqual(report["restored_from_recycle"], [])
            self.assertFalse(os.path.isdir(os.path.join(root, draft_name)))
            self.assertTrue(os.path.isdir(recycle_draft_dir))

            with open(os.path.join(root, "root_meta_info.json"), "r", encoding="utf-8") as f:
                root_meta = json.load(f)
            self.assertEqual(root_meta["all_draft_store"], [])

    def test_file_lock_cleans_stale_lock(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = os.path.join(root, ".stale.lock")
            with open(lock_path, "w", encoding="ascii") as f:
                f.write("999999 0")

            with file_lock(lock_path, timeout=0.5, poll_interval=0.01):
                self.assertTrue(os.path.exists(lock_path))

            self.assertFalse(os.path.exists(lock_path))


class SpeechVideoFilterTests(unittest.TestCase):
    def test_scan_video_file_paths_only_returns_video_files(self):
        with tempfile.TemporaryDirectory() as root:
            names = [
                "口播1.mp4",
                "口播2.mov",
                "口播3.avi",
                "口播4.wmv",
                "口播5.mkv",
                "口播6.m4v",
                "口播1.wav",
                "口播2.mp3",
                "口播3.m4a",
                "说明.txt",
            ]
            for name in names:
                with open(os.path.join(root, name), "wb") as f:
                    f.write(b"test")

            video_paths, skipped_audio_paths = scan_video_file_paths(root, recursive=False)

            self.assertEqual(len(video_paths), 6)
            self.assertEqual(len(skipped_audio_paths), 3)
            self.assertTrue(all(path.lower().endswith((".mp4", ".mov", ".avi", ".wmv", ".mkv", ".m4v")) for path in video_paths))
            self.assertTrue(all(path.lower().endswith((".wav", ".mp3", ".m4a")) for path in skipped_audio_paths))

    def test_validate_speech_video_file_rejects_audio(self):
        is_valid, message = validate_speech_video_file("demo.wav")
        self.assertFalse(is_valid)
        self.assertIn("已跳过", message)

    def test_collect_video_files_in_mixed_folder_counts_only_videos(self):
        with tempfile.TemporaryDirectory() as root:
            filenames = [
                "视频1.mp4",
                "视频2.mov",
                "视频3.avi",
                "视频4.wmv",
                "视频5.mkv",
                "视频6.m4v",
                "音频1.wav",
                "音频2.mp3",
                "音频3.aac",
            ]
            for filename in filenames:
                with open(os.path.join(root, filename), "wb") as f:
                    f.write(b"fake-media")

            from unittest.mock import patch

            with patch("otc_promo_workflow._probe_media_duration", return_value=12.5):
                videos = collect_video_files(root, log_skipped_audio=True, source_label="口播")

            self.assertEqual(len(videos), 6)
            self.assertEqual({video["filename"] for video in videos}, {"视频1.mp4", "视频2.mov", "视频3.avi", "视频4.wmv", "视频5.mkv", "视频6.m4v"})


class BrollPlacementTests(unittest.TestCase):
    def test_smart_material_matching_respects_semantic_positions(self):
        subtitles = [
            {"start": 0.0, "end": 2.8, "text": "皮肤瘙痒红斑反复", "semantic_type": "symptom", "emotional_tone": "negative"},
            {"start": 3.0, "end": 5.6, "text": "脱屑起皮很困扰", "semantic_type": "symptom", "emotional_tone": "negative"},
            {"start": 6.8, "end": 9.2, "text": "百癣夏塔热胶囊帮助改善", "semantic_type": "product", "emotional_tone": "positive"},
            {"start": 9.4, "end": 12.2, "text": "正规OTC认证更安心", "semantic_type": "product", "emotional_tone": "positive"},
            {"start": 13.8, "end": 16.4, "text": "手足体癣瘙痒别再拖", "semantic_type": "symptom", "emotional_tone": "negative"},
        ]
        product_videos = [
            {"path": "product_1.mp4", "duration": 5.0, "unique_id": "product_1", "filename": "product_1.mp4"},
            {"path": "product_2.mp4", "duration": 4.5, "unique_id": "product_2", "filename": "product_2.mp4"},
        ]
        symptom_videos = [
            {"path": "symptom_1.mp4", "duration": 5.0, "unique_id": "symptom_1", "filename": "symptom_1.mp4"},
            {"path": "symptom_2.mp4", "duration": 4.2, "unique_id": "symptom_2", "filename": "symptom_2.mp4"},
        ]

        from unittest.mock import patch

        tracker = UsageTracker({"broll": 0, "ad_review": 0, "sticker": 0})
        with tempfile.TemporaryDirectory() as output_dir:
            with patch("otc_promo_workflow.OUTPUT_DIR", output_dir):
                matches, _, _ = smart_material_matching(
                    subtitles=subtitles,
                    product_videos=product_videos,
                    symptom_videos=symptom_videos,
                    sensitivity="medium",
                    video_duration=16.4,
                    video_id="semantic_test",
                    tracker=tracker,
                )

        self.assertGreaterEqual(len(matches), 3)
        self.assertTrue(any(match["material_type"] == "产品展示" for match in matches))
        self.assertTrue(any(match["material_type"] == "病症困扰" for match in matches))

        for index in range(1, len(matches)):
            self.assertGreaterEqual(matches[index]["start_time"], matches[index - 1]["end_time"] + 0.44)

        for match in matches:
            if match["material_type"] == "产品展示":
                self.assertEqual(match["semantic_type"], "product")
            if match["material_type"] == "病症困扰":
                self.assertEqual(match["semantic_type"], "symptom")

    def test_smart_material_matching_llm_candidates_are_normalized_with_gaps(self):
        subtitles = [
            {"start": 0.0, "end": 3.0, "text": "皮肤发痒脱屑", "semantic_type": "symptom", "emotional_tone": "negative"},
            {"start": 3.4, "end": 6.2, "text": "红斑反复出现", "semantic_type": "symptom", "emotional_tone": "negative"},
            {"start": 7.0, "end": 9.8, "text": "产品介绍与改善方案", "semantic_type": "product", "emotional_tone": "positive"},
            {"start": 10.0, "end": 12.6, "text": "产品认证与使用方法", "semantic_type": "product", "emotional_tone": "positive"},
        ]
        product_videos = [{"path": "product_1.mp4", "duration": 5.0, "unique_id": "product_1", "filename": "product_1.mp4"}]
        symptom_videos = [{"path": "symptom_1.mp4", "duration": 5.0, "unique_id": "symptom_1", "filename": "symptom_1.mp4"}]
        llm_plan = {
            "b_rolls": [
                {"start": 0.2, "end": 3.2, "type": "symptom", "reason": "病症描述"},
                {"start": 3.2, "end": 6.0, "type": "symptom", "reason": "连续病症描述"},
                {"start": 7.2, "end": 10.5, "type": "product", "reason": "产品介绍"},
            ],
            "sfx": [],
            "bgm_emotion": "positive",
        }

        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as output_dir:
            with patch("otc_promo_workflow.OUTPUT_DIR", output_dir):
                with patch("otc_promo_workflow.os.environ.get", side_effect=lambda key, default="": "mock-key" if key == "LLM_API_KEY" else default):
                    with patch("llm_clip_matcher.generate_editing_plan_with_llm", return_value=llm_plan):
                        matches, _, _ = smart_material_matching(
                            subtitles=subtitles,
                            product_videos=product_videos,
                            symptom_videos=symptom_videos,
                            sensitivity="medium",
                            video_duration=12.6,
                            video_id="llm_gap_test",
                            tracker=None,
                        )

        self.assertGreaterEqual(len(matches), 2)
        for index in range(1, len(matches)):
            self.assertGreaterEqual(matches[index]["start_time"], matches[index - 1]["end_time"] + 0.44)

    def test_smart_material_matching_creates_denser_broll_for_long_blocks(self):
        subtitles = [
            {"start": 0.0, "end": 6.0, "text": "皮肤发痒反复脱屑，影响生活", "semantic_type": "symptom", "emotional_tone": "negative"},
            {"start": 6.3, "end": 12.5, "text": "产品介绍功效与适用人群", "semantic_type": "product", "emotional_tone": "positive"},
            {"start": 13.0, "end": 19.5, "text": "病症改善与复发困扰说明", "semantic_type": "symptom", "emotional_tone": "negative"},
        ]
        product_videos = [{"path": "product_1.mp4", "duration": 5.0, "unique_id": "product_1", "filename": "product_1.mp4"}]
        symptom_videos = [{"path": "symptom_1.mp4", "duration": 5.0, "unique_id": "symptom_1", "filename": "symptom_1.mp4"}]

        with tempfile.TemporaryDirectory() as output_dir:
            tracker = UsageTracker({"broll": 0, "ad_review": 0, "sticker": 0})
            from unittest.mock import patch
            with patch("otc_promo_workflow.OUTPUT_DIR", output_dir):
                matches, _, _ = smart_material_matching(
                    subtitles=subtitles,
                    product_videos=product_videos,
                    symptom_videos=symptom_videos,
                    sensitivity="medium",
                    video_duration=19.5,
                    video_id="dense_broll_test",
                    tracker=tracker,
                )

        self.assertGreaterEqual(len(matches), 5)

    def test_smart_material_matching_guarantees_product_match_when_product_material_exists(self):
        subtitles = [
            {"start": 0.0, "end": 4.2, "text": "皮肤瘙痒脱屑反复发作", "semantic_type": "symptom", "emotional_tone": "negative"},
            {"start": 4.6, "end": 8.8, "text": "红斑扩散影响睡眠和生活", "semantic_type": "symptom", "emotional_tone": "negative"},
            {"start": 9.2, "end": 13.4, "text": "拖得越久越容易反复", "semantic_type": "symptom", "emotional_tone": "negative"},
        ]
        product_videos = [{"path": "product_1.mp4", "duration": 5.0, "unique_id": "product_1", "filename": "product_1.mp4"}]
        symptom_videos = [{"path": "symptom_1.mp4", "duration": 5.0, "unique_id": "symptom_1", "filename": "symptom_1.mp4"}]

        with tempfile.TemporaryDirectory() as output_dir:
            tracker = UsageTracker({"broll": 0, "ad_review": 0, "sticker": 0})
            from unittest.mock import patch
            with patch("otc_promo_workflow.OUTPUT_DIR", output_dir):
                matches, _, _ = smart_material_matching(
                    subtitles=subtitles,
                    product_videos=product_videos,
                    symptom_videos=symptom_videos,
                    sensitivity="medium",
                    video_duration=13.4,
                    video_id="product_coverage_test",
                    tracker=tracker,
                )

        self.assertTrue(any(match["material_type"] == "产品展示" for match in matches))


class EncodingRepairTests(unittest.TestCase):
    def test_decode_process_output_recovers_utf8_console_bytes(self):
        original = "[SKIP] 跳过口播音频文件: 4月9日(9)_audio.wav"
        decoded = decode_process_output(original.encode("utf-8"))

        self.assertEqual(decoded, original)

    def test_repair_mojibake_text_keeps_normal_text_unchanged(self):
        original = "[SKIP] 跳过口播音频文件: 4月9日(9)_audio.wav"
        repaired = repair_mojibake_text(original)
        self.assertEqual(repaired, original)


class MaterialPoolRuleTests(unittest.TestCase):
    def test_materials_with_different_filenames_are_not_considered_similar(self):
        left = {
            "filename": "product_demo_a.mp4",
            "path": "D:/materials/product_demo_a.mp4",
            "unique_id": "same",
            "content_hash": "samehash",
            "duration": 4.0,
            "file_size": 1000,
        }
        right = {
            "filename": "product_demo_b.mp4",
            "path": "D:/materials/product_demo_b.mp4",
            "unique_id": "same",
            "content_hash": "samehash",
            "duration": 4.0,
            "file_size": 1000,
        }

        self.assertFalse(are_materials_similar(left, right))


class LlmConnectivityTests(unittest.TestCase):
    def test_test_llm_connectivity_returns_success_message(self):
        mock_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))]
        )
        mock_client = Mock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch("llm_clip_matcher.OpenAI", return_value=mock_client):
            ok, message = test_llm_connectivity(
                api_key="mock-key",
                model="deepseek-v3.2",
                base_url="https://example.test/v1",
            )

        self.assertTrue(ok)
        self.assertIn("联通成功", message)

    def test_test_llm_connectivity_requires_api_key(self):
        ok, message = test_llm_connectivity(
            api_key="",
            model="deepseek-v3.2",
            base_url="https://example.test/v1",
        )

        self.assertFalse(ok)
        self.assertIn("API Key", message)


class UiLoggingTests(unittest.TestCase):
    def test_nonfatal_issue_is_written_to_internal_log(self):
        with tempfile.TemporaryDirectory() as root:
            dummy_ui = SimpleNamespace(
                internal_log_path=os.path.join(root, "internal_maintenance.log"),
                maintenance_warning_count=0,
                latest_maintenance_warning="当前无维护告警",
            )
            dummy_ui._append_internal_log = lambda stage, message, exc=None: YunFengEditorUI._append_internal_log(
                dummy_ui,
                stage,
                message,
                exc=exc,
            )
            try:
                raise RuntimeError("mock cleanup failure")
            except RuntimeError as exc:
                YunFengEditorUI._log_nonfatal_issue(
                    dummy_ui,
                    "cleanup_empty_drafts",
                    "启动时清理空草稿流程执行失败，已跳过本轮清理。",
                    exc=exc,
                )

            with open(dummy_ui.internal_log_path, "r", encoding="utf-8") as f:
                content = f.read()

        self.assertIn("cleanup_empty_drafts", content)
        self.assertIn("启动时清理空草稿流程执行失败", content)
        self.assertIn("RuntimeError: mock cleanup failure", content)
        self.assertEqual(dummy_ui.maintenance_warning_count, 1)
        self.assertIn("cleanup_empty_drafts", dummy_ui.latest_maintenance_warning)


class MaterialPoolValidationTests(unittest.TestCase):
    def _build_material(self, category: str, index: int, duration: float = 5.0):
        return {
            "path": f"{category}_{index}.mp4",
            "filename": f"{category}_{index}.mp4",
            "duration": duration,
            "unique_id": f"{category}_{index}_uid",
        }

    def test_validate_material_pools_accepts_high_density_requirements(self):
        products = [self._build_material("product", idx) for idx in range(5)]
        symptoms = [self._build_material("symptom", idx) for idx in range(5)]

        deduped_products, deduped_symptoms, report = validate_material_pools(products, symptoms, "high")

        self.assertEqual(len(deduped_products), 5)
        self.assertEqual(len(deduped_symptoms), 5)
        self.assertEqual(report["total_after"], 10)

    def test_validate_material_pools_rejects_similar_duplicates(self):
        products = [
            self._build_material("product", 1),
            {"path": "product_1_copy.mp4", "filename": "product_1_copy.mp4", "duration": 5.0, "unique_id": "product_1_uid"},
            self._build_material("product", 2),
            self._build_material("product", 3),
            self._build_material("product", 4),
        ]
        symptoms = [self._build_material("symptom", idx) for idx in range(5)]

        with self.assertRaisesRegex(ValueError, "产品展示素材至少需要 5 个独立素材"):
            validate_material_pools(products, symptoms, "high")

    def test_validate_material_pools_rejects_removed_low_density_option(self):
        products = [self._build_material("product", idx) for idx in range(5)]
        symptoms = [self._build_material("symptom", idx) for idx in range(5)]

        with self.assertRaisesRegex(ValueError, "不支持的素材密度选项"):
            validate_material_pools(products, symptoms, "low")

    def test_validate_material_pools_rejects_same_content_hash(self):
        products = [
            {"path": "product_a.mp4", "filename": "product_a.mp4", "duration": 5.0, "unique_id": "a", "content_hash": "same", "file_size": 1000},
            {"path": "product_b.mp4", "filename": "product_b.mp4", "duration": 5.1, "unique_id": "b", "content_hash": "same", "file_size": 1001},
            {"path": "product_c.mp4", "filename": "product_c.mp4", "duration": 5.0, "unique_id": "c", "content_hash": "c", "file_size": 1002},
            {"path": "product_d.mp4", "filename": "product_d.mp4", "duration": 5.0, "unique_id": "d", "content_hash": "d", "file_size": 1003},
            {"path": "product_e.mp4", "filename": "product_e.mp4", "duration": 5.0, "unique_id": "e", "content_hash": "e", "file_size": 1004},
        ]
        symptoms = [self._build_material("symptom", idx) for idx in range(5)]
        for index, symptom in enumerate(symptoms):
            symptom["content_hash"] = f"symptom_{index}"
            symptom["file_size"] = 2000 + index

        with self.assertRaisesRegex(ValueError, "产品展示素材至少需要 5 个独立素材"):
            validate_material_pools(products, symptoms, "high")


class BatchConcurrencyTests(unittest.TestCase):
    def test_batch_runtime_concurrency_is_fixed_to_five(self):
        self.assertEqual(BATCH_CONCURRENCY, 5)

    def test_batch_process_uses_thread_pool_with_configured_concurrency(self):
        import batch_otc_promo_workflow as batch_module
        from unittest.mock import patch

        created_executors = []

        class FakeFuture:
            def __init__(self, result):
                self._result = result

            def result(self):
                return self._result

        class FakeExecutor:
            def __init__(self, max_workers):
                created_executors.append(max_workers)
                self.futures = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def submit(self, fn, *args, **kwargs):
                future = FakeFuture(fn(*args, **kwargs))
                self.futures.append(future)
                return future

        speech_videos = [
            {"path": f"speech_{idx}.mp4", "filename": f"speech_{idx}.mp4", "duration": 12.0, "unique_id": f"speech_{idx}", "content_hash": f"speech_hash_{idx}", "file_size": 1000 + idx}
            for idx in range(3)
        ]
        product_videos = [
            {"path": f"product_{idx}.mp4", "filename": f"product_{idx}.mp4", "duration": 5.0, "unique_id": f"product_{idx}", "content_hash": f"product_hash_{idx}", "file_size": 2000 + idx}
            for idx in range(5)
        ]
        symptom_videos = [
            {"path": f"symptom_{idx}.mp4", "filename": f"symptom_{idx}.mp4", "duration": 5.0, "unique_id": f"symptom_{idx}", "content_hash": f"symptom_hash_{idx}", "file_size": 3000 + idx}
            for idx in range(5)
        ]

        with tempfile.TemporaryDirectory() as output_dir:
            with patch.object(batch_module, "collect_video_files", side_effect=[speech_videos, product_videos, symptom_videos]):
                with patch.object(batch_module, "write_material_pool_report"):
                    with patch.object(batch_module, "MATERIAL_POOL_REPORT_PATH", os.path.join(output_dir, "material_pool_validation.json")):
                        with patch.object(batch_module, "_process_single_speech_video", side_effect=lambda speech_video, *_args: {
                            "video": speech_video["filename"],
                            "project": f"OTC推广_{os.path.splitext(speech_video['filename'])[0]}",
                            "status": "成功",
                            "subtitles": 3,
                            "matches": 2,
                            "product_matches": 1,
                            "symptom_matches": 1,
                            "error": "",
                        }):
                            with patch.object(batch_module, "ThreadPoolExecutor", FakeExecutor):
                                with patch.object(batch_module, "as_completed", side_effect=lambda futures: list(futures)):
                                    batch_module.batch_process_otc_videos(sensitivity="high", limit=0)

        self.assertEqual(created_executors, [5])


if __name__ == "__main__":
    unittest.main()
