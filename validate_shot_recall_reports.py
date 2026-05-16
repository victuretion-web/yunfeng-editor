import glob
import json
import os
from collections import Counter, defaultdict
from typing import Dict, List

from app_paths import get_output_dir


OUTPUT_DIR = get_output_dir()
DECISION_LOG_DIR = os.path.join(OUTPUT_DIR, "decision_logs")
SUMMARY_PATH = os.path.join(OUTPUT_DIR, "shot_recall_validation_summary.json")


def _load_jsonl(path: str) -> List[Dict]:
    rows: List[Dict] = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _list_logs() -> List[str]:
    if not os.path.isdir(DECISION_LOG_DIR):
        return []
    return sorted(
        [path for path in glob.glob(os.path.join(DECISION_LOG_DIR, "*.jsonl")) if os.path.isfile(path)],
        key=os.path.getmtime,
        reverse=True,
    )


def build_summary(limit: int = 12) -> Dict[str, object]:
    log_paths = _list_logs()[: max(1, limit)]
    fallback_counter: Counter = Counter()
    trigger_counter: Counter = Counter()
    source_counter: Counter = Counter()
    per_video: List[Dict[str, object]] = []
    shot_selected_total = 0
    selection_total = 0
    shot_score_values: List[float] = []
    normal_score_values: List[float] = []
    shot_examples: Dict[str, List[Dict[str, object]]] = defaultdict(list)

    for path in log_paths:
        rows = _load_jsonl(path)
        selected = [row for row in rows if row.get("event") == "broll_material_selected"]
        shot_init = [row for row in rows if row.get("event") == "shot_recall_initialized"]
        video_counter: Counter = Counter()
        video_shot_selected = 0

        for row in selected:
            fallback_level = str(row.get("fallback_level", "")).strip() or "unknown"
            is_shot = bool(row.get("is_shot_material")) or fallback_level.endswith("_shot")
            trigger_reason = str(row.get("trigger_reason", "")).strip() or "unknown"
            semantic_score = float(row.get("semantic_score", 0.0) or 0.0)

            fallback_counter[fallback_level] += 1
            trigger_counter[trigger_reason] += 1
            source_counter["shot" if is_shot else "full_video"] += 1
            video_counter[fallback_level] += 1
            selection_total += 1

            if is_shot:
                video_shot_selected += 1
                shot_selected_total += 1
                shot_score_values.append(semantic_score)
                if len(shot_examples[os.path.basename(path)]) < 5:
                    shot_examples[os.path.basename(path)].append(
                        {
                            "material_name": row.get("material_name", ""),
                            "fallback_level": fallback_level,
                            "shot_index": row.get("shot_index"),
                            "shot_start_seconds": row.get("shot_start_seconds"),
                            "shot_end_seconds": row.get("shot_end_seconds"),
                            "shot_caption": row.get("shot_caption", ""),
                            "semantic_score": round(semantic_score, 4),
                        }
                    )
            else:
                normal_score_values.append(semantic_score)

        per_video.append(
            {
                "decision_log": path,
                "selection_count": len(selected),
                "shot_selected_count": video_shot_selected,
                "shot_selected_ratio": round(video_shot_selected / len(selected), 4) if selected else 0.0,
                "fallback_levels": dict(video_counter),
                "shot_recall_initialized": shot_init[-1] if shot_init else {},
                "shot_examples": shot_examples.get(os.path.basename(path), []),
            }
        )

    summary = {
        "log_count": len(log_paths),
        "selection_total": selection_total,
        "shot_selected_total": shot_selected_total,
        "shot_selected_ratio": round(shot_selected_total / selection_total, 4) if selection_total else 0.0,
        "fallback_levels": dict(fallback_counter),
        "trigger_reasons": dict(trigger_counter),
        "selection_sources": dict(source_counter),
        "avg_shot_score": round(sum(shot_score_values) / len(shot_score_values), 4) if shot_score_values else 0.0,
        "avg_full_video_score": round(sum(normal_score_values) / len(normal_score_values), 4) if normal_score_values else 0.0,
        "videos": per_video,
    }
    return summary


def main() -> int:
    summary = build_summary(limit=12)
    os.makedirs(os.path.dirname(SUMMARY_PATH), exist_ok=True)
    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n已写入: {SUMMARY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
