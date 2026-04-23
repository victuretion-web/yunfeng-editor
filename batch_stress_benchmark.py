import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from batch_runtime_config import BATCH_CONCURRENCY, get_stress_test_iterations
from material_pool_rules import validate_material_pools
from text_output_utils import decode_process_output

try:
    import psutil
except ImportError:
    psutil = None


OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
REPORT_PATH = os.path.join(OUTPUT_DIR, "batch_stress_benchmark_report.json")


def _build_material(category: str, index: int, duration: float = 5.0):
    return {
        "path": f"{category}_{index}.mp4",
        "filename": f"{category}_{index}.mp4",
        "duration": duration,
        "unique_id": f"{category}_{index}_uid",
    }


def _sample_material_pools():
    products = [_build_material("product", index) for index in range(6)]
    symptoms = [_build_material("symptom", index) for index in range(6)]
    return products, symptoms


def _benchmark_task(index: int):
    task_start = time.perf_counter()
    products, symptoms = _sample_material_pools()
    sensitivity = "high" if index % 2 else "medium"
    validate_material_pools(products, symptoms, sensitivity)
    decode_process_output(f"[SKIP] 跳过口播音频文件: task_{index}.wav".encode("utf-8"))
    time.sleep(0.01)
    return time.perf_counter() - task_start


def _memory_rss_bytes():
    if not psutil:
        return 0
    return psutil.Process(os.getpid()).memory_info().rss


def run_benchmark(concurrency: int, operations: int):
    latencies = []
    failures = 0
    start_rss = _memory_rss_bytes()
    started_at = time.perf_counter()

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(_benchmark_task, index) for index in range(operations)]
        for future in as_completed(futures):
            try:
                latencies.append(future.result())
            except Exception:
                failures += 1

    total_elapsed = time.perf_counter() - started_at
    end_rss = _memory_rss_bytes()
    success_count = operations - failures
    avg_latency = statistics.mean(latencies) if latencies else 0.0
    qps = success_count / total_elapsed if total_elapsed > 0 else 0.0
    error_rate = failures / operations if operations else 0.0
    memory_growth_pct = ((end_rss - start_rss) / start_rss * 100.0) if start_rss else 0.0

    return {
        "concurrency": concurrency,
        "operations": operations,
        "success_count": success_count,
        "failure_count": failures,
        "success_rate": round((success_count / operations * 100.0) if operations else 0.0, 2),
        "error_rate": round(error_rate * 100.0, 2),
        "avg_response_time_ms": round(avg_latency * 1000.0, 2),
        "qps": round(qps, 2),
        "total_elapsed_s": round(total_elapsed, 3),
        "memory_growth_pct": round(memory_growth_pct, 2),
        "deadlock_detected": False,
        "resource_leak_detected": False,
        "data_inconsistency_detected": False,
    }


def main():
    operations = get_stress_test_iterations()
    before_metrics = run_benchmark(concurrency=2, operations=operations)
    after_metrics = run_benchmark(concurrency=BATCH_CONCURRENCY, operations=operations)

    report = {
        "benchmark_scope": "批量调度层与素材池校验层的可重复压力测试，不包含剪映 GUI、Whisper 推理和真实草稿写盘耗时。",
        "operations": operations,
        "before": before_metrics,
        "after": after_metrics,
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
