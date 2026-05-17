import csv
import json
import math
import os
from typing import Dict, List, Sequence, Tuple


def get_density_template_config(template_name: str) -> Dict[str, float]:
    mapping = {
        "快节奏": {
            "window_seconds": 30.0,
            "min_segments_per_window": 3,
            "insert_min_duration": 1.0,
            "insert_max_duration": 2.4,
        },
        "慢节奏": {
            "window_seconds": 30.0,
            "min_segments_per_window": 2,
            "insert_min_duration": 1.5,
            "insert_max_duration": 3.0,
        },
        "中节奏": {
            "window_seconds": 30.0,
            "min_segments_per_window": 2,
            "insert_min_duration": 1.0,
            "insert_max_duration": 3.0,
        },
    }
    return mapping.get(template_name or "中节奏", mapping["中节奏"]).copy()


def _segment_overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def analyze_insert_density(
    matches: Sequence[Dict],
    video_duration: float,
    window_seconds: float = 30.0,
    min_segments_per_window: int = 2,
) -> List[Dict]:
    if video_duration <= 0:
        return []

    windows: List[Dict] = []
    total_windows = max(1, int(math.ceil(video_duration / window_seconds)))
    for idx in range(total_windows):
        start = idx * window_seconds
        end = min(video_duration, start + window_seconds)
        overlapping = []
        for match in matches:
            overlap = _segment_overlap(
                float(match.get("start_time", 0.0)),
                float(match.get("end_time", 0.0)),
                start,
                end,
            )
            if overlap > 0.1:
                overlapping.append(match)
        windows.append(
            {
                "window_index": idx + 1,
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
                "insert_count": len(overlapping),
                "status": "达标" if len(overlapping) >= min_segments_per_window else "待补齐",
                "required": min_segments_per_window,
            }
        )
    return windows



def _build_synthetic_window_candidate(
    window_start: float,
    window_end: float,
    slot_index: int,
    slot_count: int,
    min_duration: float,
    max_duration: float,
    semantic_type: str,
    text: str = "密度补齐",
) -> Dict:
    span = max(0.0, window_end - window_start)
    desired_duration = min(max_duration, max(min_duration, min(span * 0.55, max_duration)))
    center = window_start + ((slot_index + 1) / (slot_count + 1)) * span
    start = max(window_start, center - desired_duration / 2.0)
    end = min(window_end, start + desired_duration)
    if end - start < min_duration:
        start = max(window_start, end - min_duration)
        end = min(window_end, start + min_duration)
    return {
        "start_time": round(start, 3),
        "end_time": round(end, 3),
        "duration": round(max(0.0, end - start), 3),
        "semantic_type": semantic_type,
        "text": text,
        "is_transition": False,
        "is_density_fill": True,
    }


def _segment_conflicts(existing_items: Sequence[Dict], start: float, end: float, threshold: float = 0.3) -> bool:
    for existing in existing_items:
        if _segment_overlap(
            float(existing.get("start_time", 0.0)),
            float(existing.get("end_time", 0.0)),
            start,
            end,
        ) > threshold:
            return True
    return False


def _choose_fill_semantic_type(existing_items: Sequence[Dict], window_start: float, window_end: float) -> str:
    product_count = 0
    symptom_count = 0
    for item in existing_items:
        if _segment_overlap(
            float(item.get("start_time", 0.0)),
            float(item.get("end_time", 0.0)),
            window_start,
            window_end,
        ) <= 0.1:
            continue
        semantic_type = item.get("semantic_type")
        if semantic_type == "product":
            product_count += 1
        elif semantic_type == "symptom":
            symptom_count += 1
    return "symptom" if symptom_count <= product_count else "product"


def auto_fill_density_gaps(
    candidate_matches: Sequence[Dict],
    video_duration: float,
    window_seconds: float,
    min_segments_per_window: int,
    insert_min_duration: float,
    insert_max_duration: float,
    target_ratio: float = 0.65,
) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    merged = [dict(item) for item in candidate_matches]
    fill_actions: List[Dict] = []
    windows = analyze_insert_density(
        merged,
        video_duration=video_duration,
        window_seconds=window_seconds,
        min_segments_per_window=min_segments_per_window,
    )

    for window in windows:
        if window["status"] == "达标":
            continue

        need_count = max(0, int(window["required"]) - int(window["insert_count"]))
        if need_count <= 0:
            continue

        window_start = float(window["start"])
        window_end = float(window["end"])

        added_count = 0
        retries = 0
        max_retries = need_count * 3
        while added_count < need_count and retries < max_retries:
            candidate = _build_synthetic_window_candidate(
                window_start=window_start,
                window_end=window_end,
                slot_index=added_count + retries,
                slot_count=need_count + retries,
                min_duration=insert_min_duration,
                max_duration=insert_max_duration,
                semantic_type=_choose_fill_semantic_type(merged, window_start, window_end),
            )
            if candidate["duration"] < insert_min_duration - 0.05:
                break
            if _segment_conflicts(merged, candidate["start_time"], candidate["end_time"]):
                retries += 1
                continue
            merged.append(candidate)
            fill_actions.append(
                {
                    "window_index": window["window_index"],
                    "window_start": window["start"],
                    "window_end": window["end"],
                    "text": candidate["text"],
                    "semantic_type": candidate["semantic_type"],
                    "start_time": candidate["start_time"],
                    "end_time": candidate["end_time"],
                    "duration": candidate["duration"],
                }
            )
            added_count += 1

    # 全局覆盖率补齐：若窗口补齐后仍未达 target_ratio，在密度最低的窗口追加中插
    if target_ratio > 0 and video_duration > 0:
        max_iterations = 50
        iteration = 0
        while iteration < max_iterations:
            iteration += 1
            total_insert = sum(
                float(item.get("end_time", 0.0)) - float(item.get("start_time", 0.0))
                for item in merged
            )
            current_ratio = total_insert / video_duration
            if current_ratio >= target_ratio:
                break

            # 找到覆盖率最低的窗口
            windows_check = analyze_insert_density(
                merged,
                video_duration=video_duration,
                window_seconds=window_seconds,
                min_segments_per_window=min_segments_per_window,
            )
            # 计算每个窗口的已覆盖时长
            window_covered: Dict[int, float] = {}
            for item in merged:
                item_start = float(item.get("start_time", 0.0))
                item_end = float(item.get("end_time", 0.0))
                for w in windows_check:
                    ws = float(w["start"])
                    we = float(w["end"])
                    if _segment_overlap(item_start, item_end, ws, we) > 0.1:
                        window_covered[w["window_index"]] = window_covered.get(w["window_index"], 0) + (item_end - item_start)

            # 按可用空间从大到小排列窗口，逐个尝试直到成功插入
            candidate_windows = []
            for w in windows_check:
                ws = float(w["start"])
                we = float(w["end"])
                covered = window_covered.get(w["window_index"], 0)
                space = (we - ws) - covered
                if space >= insert_min_duration:
                    candidate_windows.append((space, w))

            if not candidate_windows:
                break

            candidate_windows.sort(key=lambda item: item[0], reverse=True)
            inserted = False
            for best_space, best_window in candidate_windows:
                ws = float(best_window["start"])
                we = float(best_window["end"])
                candidate = _build_synthetic_window_candidate(
                    window_start=ws,
                    window_end=we,
                    slot_index=int(best_window["insert_count"]),
                    slot_count=max(1, int(best_window["insert_count"]) + 1),
                    min_duration=insert_min_duration,
                    max_duration=insert_max_duration,
                    semantic_type=_choose_fill_semantic_type(merged, ws, we),
                )
                if candidate["duration"] < insert_min_duration - 0.05:
                    continue
                if _segment_conflicts(merged, candidate["start_time"], candidate["end_time"]):
                    continue
                merged.append(candidate)
                fill_actions.append(
                    {
                        "window_index": best_window["window_index"],
                        "window_start": best_window["start"],
                        "window_end": best_window["end"],
                        "text": candidate["text"],
                        "semantic_type": candidate["semantic_type"],
                        "start_time": candidate["start_time"],
                        "end_time": candidate["end_time"],
                        "duration": candidate["duration"],
                    }
                )
                inserted = True
                break

            if not inserted:
                break

    if target_ratio > 0 and video_duration > 0:
        total_insert = sum(
            float(item.get("end_time", 0.0)) - float(item.get("start_time", 0.0))
            for item in merged
        )
        final_ratio = total_insert / video_duration
        if final_ratio < target_ratio - 0.03:
            print(f"   [!] 密度补齐：目标覆盖率 {target_ratio:.0%}，实际 {final_ratio:.1%}，已尽力补齐。")

    merged.sort(key=lambda item: float(item.get("start_time", 0.0)))
    final_windows = analyze_insert_density(
        merged,
        video_duration=video_duration,
        window_seconds=window_seconds,
        min_segments_per_window=min_segments_per_window,
    )
    return merged, final_windows if fill_actions else windows, fill_actions


def export_density_reports(
    video_id: str,
    output_dir: str,
    matches: Sequence[Dict],
    density_windows: Sequence[Dict],
    fill_actions: Sequence[Dict],
) -> Dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, f"{video_id}_中插密度报告.json")
    csv_path = os.path.join(output_dir, f"{video_id}_中插密度报告.csv")

    payload = {
        "video_id": video_id,
        "segments": [
            {
                "start": round(float(item.get("start_time", 0.0)), 3),
                "end": round(float(item.get("end_time", 0.0)), 3),
                "duration": round(float(item.get("duration", 0.0)), 3),
                "type": item.get("material_type", item.get("semantic_type", "")),
                "auto_fill": bool(item.get("is_density_fill", False)),
            }
            for item in matches
        ],
        "windows": list(density_windows),
        "fill_actions": list(fill_actions),
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["窗口序号", "开始", "结束", "中插数", "要求", "状态"])
        for item in density_windows:
            writer.writerow(
                [
                    item.get("window_index"),
                    item.get("start"),
                    item.get("end"),
                    item.get("insert_count"),
                    item.get("required"),
                    item.get("status"),
                ]
            )
    return {"json_path": json_path, "csv_path": csv_path}
