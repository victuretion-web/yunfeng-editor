import os


BATCH_CONCURRENCY = 5
SUBPROCESS_SLOT_LIMIT = BATCH_CONCURRENCY
LLM_CONNECTION_POOL_LIMIT = BATCH_CONCURRENCY
TASK_QUEUE_CAPACITY = max(100, BATCH_CONCURRENCY * 20)
BATCH_RETRY_LIMIT = 2
STRESS_TEST_OPERATION_COUNT = 100


def get_stress_test_iterations() -> int:
    value = os.environ.get("OTC_STRESS_TEST_OPERATIONS", "").strip()
    if not value:
        return STRESS_TEST_OPERATION_COUNT
    try:
        parsed = int(value)
    except ValueError:
        return STRESS_TEST_OPERATION_COUNT
    return max(1, parsed)
