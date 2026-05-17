import os


DEFAULT_BATCH_CONCURRENCY = 5
DEFAULT_BATCH_RETRY_LIMIT = 2
DEFAULT_STRESS_TEST_OPERATION_COUNT = 100


def _parse_env_int(name: str, default: int, minimum: int = 1) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(minimum, parsed)


def get_batch_concurrency() -> int:
    return _parse_env_int("OTC_BATCH_CONCURRENCY", DEFAULT_BATCH_CONCURRENCY, minimum=1)


def get_subprocess_slot_limit() -> int:
    return get_batch_concurrency()


def get_llm_connection_pool_limit() -> int:
    return get_batch_concurrency()


def get_task_queue_capacity() -> int:
    return max(100, get_batch_concurrency() * 20)


def get_batch_retry_limit() -> int:
    return _parse_env_int("OTC_BATCH_RETRY_LIMIT", DEFAULT_BATCH_RETRY_LIMIT, minimum=0)


def get_runtime_limits() -> dict[str, int]:
    batch_concurrency = get_batch_concurrency()
    return {
        "batch_concurrency": batch_concurrency,
        "subprocess_slot_limit": batch_concurrency,
        "llm_connection_pool_limit": batch_concurrency,
        "task_queue_capacity": max(100, batch_concurrency * 20),
        "batch_retry_limit": get_batch_retry_limit(),
    }

def get_stress_test_iterations() -> int:
    value = os.environ.get("OTC_STRESS_TEST_OPERATIONS", "").strip()
    if not value:
        return DEFAULT_STRESS_TEST_OPERATION_COUNT
    try:
        parsed = int(value)
    except ValueError:
        return DEFAULT_STRESS_TEST_OPERATION_COUNT
    return max(1, parsed)
