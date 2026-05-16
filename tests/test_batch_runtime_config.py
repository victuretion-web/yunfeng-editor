import os
import unittest
from unittest.mock import patch

import batch_runtime_config


class BatchRuntimeConfigTests(unittest.TestCase):
    def test_get_batch_concurrency_uses_default_for_invalid_value(self):
        with patch.dict(os.environ, {"OTC_BATCH_CONCURRENCY": "bad"}, clear=False):
            self.assertEqual(batch_runtime_config.get_batch_concurrency(), 5)

    def test_get_batch_concurrency_respects_positive_value(self):
        with patch.dict(os.environ, {"OTC_BATCH_CONCURRENCY": "8"}, clear=False):
            self.assertEqual(batch_runtime_config.get_batch_concurrency(), 8)

    def test_get_runtime_limits_expands_queue_capacity(self):
        with patch.dict(os.environ, {"OTC_BATCH_CONCURRENCY": "7"}, clear=False):
            limits = batch_runtime_config.get_runtime_limits()
        self.assertEqual(limits["batch_concurrency"], 7)
        self.assertEqual(limits["subprocess_slot_limit"], 7)
        self.assertEqual(limits["llm_connection_pool_limit"], 7)
        self.assertEqual(limits["task_queue_capacity"], 140)

    def test_get_batch_retry_limit_allows_zero(self):
        with patch.dict(os.environ, {"OTC_BATCH_RETRY_LIMIT": "0"}, clear=False):
            self.assertEqual(batch_runtime_config.get_batch_retry_limit(), 0)


if __name__ == "__main__":
    unittest.main()
