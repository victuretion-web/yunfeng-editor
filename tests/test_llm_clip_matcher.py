import unittest
from types import ModuleType
from unittest.mock import patch

import llm_clip_matcher


class LlmClipMatcherTests(unittest.TestCase):
    def test_normalize_model_name_uses_siliconflow_default(self):
        self.assertEqual(
            llm_clip_matcher.normalize_model_name(""),
            "deepseek-ai/DeepSeek-V4-Flash",
        )

    def test_build_chat_completion_endpoints_with_v1_base(self):
        endpoints = llm_clip_matcher._build_chat_completion_endpoints("https://api.kuai.host/v1")
        self.assertEqual(
            endpoints,
            [
                "https://api.kuai.host/v1/chat/completions",
                "https://api.kuai.host/chat/completions",
            ],
        )

    def test_build_chat_completion_endpoints_with_plain_base(self):
        endpoints = llm_clip_matcher._build_chat_completion_endpoints("https://api.kuai.host")
        self.assertEqual(
            endpoints,
            [
                "https://api.kuai.host/v1/chat/completions",
                "https://api.kuai.host/chat/completions",
            ],
        )

    def test_build_chat_completion_endpoints_preserves_full_endpoint(self):
        endpoints = llm_clip_matcher._build_chat_completion_endpoints(
            "https://gateway.example.com/openai/v1/chat/completions"
        )
        self.assertEqual(endpoints, ["https://gateway.example.com/openai/v1/chat/completions"])

    def test_extract_response_text_falls_back_to_reasoning_content(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": "这是一张测试图片，联通正常。",
                    }
                }
            ]
        }
        self.assertEqual(
            llm_clip_matcher._extract_response_text(payload),
            "这是一张测试图片，联通正常。",
        )

    def test_keyword_planner_prefers_real_keyword_anchors_without_density_fill(self):
        plan = llm_clip_matcher.plan_insertions_with_keywords(
            transcript_segments=[
                {"start": 1.0, "end": 2.4, "text": "这个药膏可以缓解瘙痒"},
                {"start": 8.0, "end": 9.2, "text": "晚上总是发痒睡不好"},
            ],
            video_duration=30.0,
        )

        self.assertEqual(len(plan["b_rolls"]), 2)
        self.assertEqual(plan["b_rolls"][0]["type"], "product")
        self.assertEqual(plan["b_rolls"][0]["keyword"], "药膏")
        self.assertEqual(plan["b_rolls"][1]["type"], "symptom")
        self.assertEqual(plan["b_rolls"][1]["keyword"], "发痒")

    def test_call_chat_completions_http_falls_back_to_second_suffix(self):
        class _Response:
            def __init__(self, status_code, content_type, text, payload=None):
                self.status_code = status_code
                self.headers = {"Content-Type": content_type}
                self.text = text
                self._payload = payload or {}

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            def json(self):
                return self._payload

        class _RequestException(Exception):
            pass

        calls = []

        def _fake_post(url, **kwargs):
            calls.append(url)
            if url.endswith("/v1/chat/completions"):
                return _Response(200, "text/html", "<html>wrong route</html>")
            return _Response(
                200,
                "application/json",
                '{"choices":[{"message":{"content":"ok"}}]}',
                payload={"choices": [{"message": {"content": "ok"}}]},
            )

        fake_requests = ModuleType("requests")
        fake_requests.post = _fake_post
        fake_requests.RequestException = _RequestException

        with patch.dict("sys.modules", {"requests": fake_requests}):
            payload = llm_clip_matcher._call_chat_completions_http(
                api_key="k",
                model="deepseek-v3.2",
                base_url="https://api.kuai.host/v1",
                messages=[{"role": "user", "content": "ping"}],
            )

        self.assertEqual(payload["choices"][0]["message"]["content"], "ok")
        self.assertEqual(
            calls,
            [
                "https://api.kuai.host/v1/chat/completions",
                "https://api.kuai.host/chat/completions",
            ],
        )


if __name__ == "__main__":
    unittest.main()
