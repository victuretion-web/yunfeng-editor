from typing import Dict, List, Tuple


MICROSECONDS_PER_SECOND = 1_000_000


def seconds_to_microseconds(value: float) -> int:
    return int(round(float(value) * MICROSECONDS_PER_SECOND))


def microseconds_to_seconds(value: int) -> float:
    return value / MICROSECONDS_PER_SECOND


def sanitize_non_overlapping_segments(
    segments: List[Dict],
    total_duration: float,
    start_key: str = "start_time",
    end_key: str = "end_time",
    min_duration: float = 0.35,
) -> Tuple[List[Dict], Dict]:
    cleaned: List[Dict] = []
    dropped = 0
    shifted = 0
    last_end_us = 0
    total_duration_us = seconds_to_microseconds(total_duration)
    min_duration_us = seconds_to_microseconds(min_duration)

    for seg in sorted(segments, key=lambda item: float(item.get(start_key, 0.0))):
        start_us = max(0, seconds_to_microseconds(seg.get(start_key, 0.0)))
        end_us = min(total_duration_us, seconds_to_microseconds(seg.get(end_key, seg.get(start_key, 0.0))))
        if end_us <= start_us:
            end_us = min(total_duration_us, start_us + seconds_to_microseconds(seg.get("duration", 0.0)))

        if start_us < last_end_us:
            start_us = last_end_us
            shifted += 1

        duration_us = end_us - start_us
        if duration_us < min_duration_us:
            dropped += 1
            continue

        normalized = seg.copy()
        normalized[start_key] = round(microseconds_to_seconds(start_us), 3)
        normalized[end_key] = round(microseconds_to_seconds(end_us), 3)
        normalized["duration"] = round(microseconds_to_seconds(duration_us), 3)
        normalized["start_us"] = start_us
        normalized["end_us"] = end_us
        normalized["duration_us"] = duration_us
        cleaned.append(normalized)
        last_end_us = end_us

    stats = {
        "input_count": len(segments),
        "output_count": len(cleaned),
        "shifted_count": shifted,
        "dropped_count": dropped,
    }
    return cleaned, stats


def layout_segments_on_tracks(
    segments: List[Dict],
    total_duration: float,
    start_key: str = "start",
    end_key: str = "end",
    min_duration: float = 0.05,
) -> Tuple[List[Dict], Dict]:
    laid_out: List[Dict] = []
    dropped = 0
    shifted = 0
    track_end_times_us: List[int] = []
    total_duration_us = seconds_to_microseconds(total_duration)
    min_duration_us = seconds_to_microseconds(min_duration)

    ordered_segments = sorted(
        segments,
        key=lambda item: (
            float(item.get(start_key, 0.0)),
            float(item.get(end_key, item.get(start_key, 0.0))),
        ),
    )

    for seg in ordered_segments:
        start_us = max(0, seconds_to_microseconds(seg.get(start_key, 0.0)))
        end_us = min(total_duration_us, seconds_to_microseconds(seg.get(end_key, seg.get(start_key, 0.0))))
        if end_us <= start_us:
            fallback_duration = seconds_to_microseconds(seg.get("duration", 0.0))
            end_us = min(total_duration_us, start_us + fallback_duration)

        if end_us - start_us < min_duration_us:
            dropped += 1
            continue

        target_track_index = None
        for idx, track_end_us in enumerate(track_end_times_us):
            if start_us >= track_end_us:
                target_track_index = idx
                break

        if target_track_index is None:
            target_track_index = len(track_end_times_us)
            track_end_times_us.append(0)

        if start_us < track_end_times_us[target_track_index]:
            start_us = track_end_times_us[target_track_index]
            shifted += 1

        duration_us = end_us - start_us
        if duration_us < min_duration_us:
            dropped += 1
            continue

        normalized = seg.copy()
        normalized[start_key] = round(microseconds_to_seconds(start_us), 3)
        normalized[end_key] = round(microseconds_to_seconds(end_us), 3)
        normalized["duration"] = round(microseconds_to_seconds(duration_us), 3)
        normalized["start_us"] = start_us
        normalized["end_us"] = end_us
        normalized["duration_us"] = duration_us
        normalized["track_index"] = target_track_index
        laid_out.append(normalized)
        track_end_times_us[target_track_index] = end_us

    stats = {
        "input_count": len(segments),
        "output_count": len(laid_out),
        "shifted_count": shifted,
        "dropped_count": dropped,
        "track_count": len(track_end_times_us),
    }
    return laid_out, stats
