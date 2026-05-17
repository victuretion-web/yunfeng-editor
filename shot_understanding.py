import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from typing import Dict, List, Optional, Tuple

import cv2

from app_paths import runtime_path
from llm_clip_matcher import describe_image_with_llm
from media_file_rules import scan_video_file_paths

SHOT_OUTPUT_ROOT = runtime_path("output", "shot_understanding")
SHOT_CACHE_ROOT = os.path.join(SHOT_OUTPUT_ROOT, "cache")
LATEST_SHOT_REPORT_PATH = os.path.join(SHOT_OUTPUT_ROOT, "latest_shot_report.json")

ROLE_CONTEXT_TAGS = {
    "product": "产品相关候选",
    "symptom": "病症相关候选",
    "speech": "口播相关候选",
    "generic": "通用镜头候选",
}

ROLE_DESCRIPTION_HINTS = {
    "product": "更偏产品相关画面",
    "symptom": "更偏病症表现或患处画面",
    "speech": "更偏人物讲解或口播画面",
    "generic": "更偏过渡或生活场景画面",
}

GENERIC_PATH_PARTS = {
    "产品", "病症", "口播", "speech", "product", "symptom", "audio", "视频", "素材", "体癣",
}

_FACE_CASCADE = None
_FACE_CASCADE_PATH = None

KEYWORD_TAGS = {
    "抓": "抓挠动作",
    "挠": "抓挠动作",
    "痒": "瘙痒困扰",
    "红": "泛红表现",
    "皮肤": "皮肤主体",
    "患处": "患处展示",
    "涂": "涂抹动作",
    "抹": "涂抹动作",
    "喷": "喷雾动作",
    "药": "产品相关",
    "膏": "产品相关",
    "包装": "包装展示",
    "盒": "包装展示",
    "手": "手部动作",
    "脚": "足部场景",
    "腿": "腿部场景",
    "夜": "夜间场景",
    "床": "居家场景",
    "浴室": "卫浴场景",
    "卫生间": "卫浴场景",
    "人物": "人物出镜",
    "讲": "口播讲解",
    "说": "口播讲解",
    "近景": "近景提示",
    "特写": "特写提示",
}


def ensure_shot_output_root() -> str:
    os.makedirs(SHOT_CACHE_ROOT, exist_ok=True)
    return SHOT_OUTPUT_ROOT


def _safe_name(text: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff\-\(\)]+", "_", str(text or "").strip(), flags=re.UNICODE)
    return cleaned.strip("_") or "untitled"


def _path_fingerprint(path: str) -> str:
    abs_path = os.path.abspath(path)
    stat = os.stat(abs_path)
    payload = f"{abs_path}|{stat.st_mtime_ns}|{stat.st_size}"
    return hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _load_json(path: str) -> Optional[Dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _write_json(path: str, payload: Dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _extract_path_evidence_text(video_path: str) -> str:
    parts = []
    normalized_parts = os.path.normpath(video_path).split(os.sep)
    for part in normalized_parts[-3:]:
        clean = str(part or "").strip()
        if not clean:
            continue
        stem = os.path.splitext(clean)[0]
        if stem in GENERIC_PATH_PARTS:
            continue
        if stem.isdigit():
            continue
        parts.append(stem)
    if not parts:
        parts.append(os.path.splitext(os.path.basename(video_path))[0])
    return " ".join(parts)


def _infer_explicit_tags(video_path: str) -> List[str]:
    text = _extract_path_evidence_text(video_path).lower()
    tags: List[str] = []
    for key, value in KEYWORD_TAGS.items():
        if key.lower() in text and value not in tags:
            tags.append(value)
    return tags[:6]


_TEMP_COPIED_FILES: List[str] = []


def _cleanup_stale_temp_copies(max_age_hours: int = 24) -> int:
    safe_root = os.path.join(tempfile.gettempdir(), "yunfeng_cv2_data")
    if not os.path.isdir(safe_root):
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for name in os.listdir(safe_root):
        filepath = os.path.join(safe_root, name)
        try:
            if os.path.isfile(filepath) and os.path.getmtime(filepath) < cutoff:
                os.remove(filepath)
                removed += 1
        except OSError:
            continue
    return removed


def _resolve_opencv_readable_path(path: str) -> str:
    if not path or not os.path.exists(path):
        return path
    try:
        path.encode("ascii")
        return path
    except UnicodeEncodeError:
        pass
    try:
        short_buffer = ctypes.create_unicode_buffer(32768)
        short_length = ctypes.windll.kernel32.GetShortPathNameW(path, short_buffer, len(short_buffer))
        if short_length:
            short_path = short_buffer.value
            if short_path and os.path.exists(short_path):
                try:
                    short_path.encode("ascii")
                    return short_path
                except UnicodeEncodeError:
                    pass
    except (AttributeError, OSError) as exc:
        print(f"   [WARN] 获取短路径失败，将尝试复制到临时目录: {os.path.basename(path)} ({exc})")
    safe_root = os.path.join(tempfile.gettempdir(), "yunfeng_cv2_data")
    os.makedirs(safe_root, exist_ok=True)
    safe_path = os.path.join(safe_root, os.path.basename(path))
    try:
        src_stat = os.stat(path)
        dst_stat = os.stat(safe_path) if os.path.exists(safe_path) else None
        if dst_stat is None or dst_stat.st_size != src_stat.st_size:
            file_size_mb = src_stat.st_size / (1024 * 1024)
            if file_size_mb > 100:
                print(f"   [WARN] 视频路径含非 ASCII 字符，正在复制大文件到临时目录 ({file_size_mb:.0f} MB): {os.path.basename(path)}")
            shutil.copy2(path, safe_path)
            _TEMP_COPIED_FILES.append(safe_path)
        # 每次经过此路径时顺便清理超过 24 小时的旧临时文件。
        _cleanup_stale_temp_copies(max_age_hours=24)
        return safe_path
    except OSError as exc:
        raise RuntimeError(
            f"复制 OpenCV 兼容路径失败，无法继续使用非 ASCII 路径: {os.path.basename(path)} ({exc})"
        ) from exc


def _get_face_cascade():
    global _FACE_CASCADE, _FACE_CASCADE_PATH
    if _FACE_CASCADE is not None:
        return _FACE_CASCADE
    try:
        cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        cascade_path = _resolve_opencv_readable_path(cascade_path)
        detector = cv2.CascadeClassifier(cascade_path)
        if detector.empty():
            _FACE_CASCADE = None
            _FACE_CASCADE_PATH = None
        else:
            _FACE_CASCADE = detector
            _FACE_CASCADE_PATH = cascade_path
    except (AttributeError, OSError, RuntimeError, cv2.error) as exc:
        print(f"   [WARN] 人脸检测器初始化失败: {exc}")
        _FACE_CASCADE = None
        _FACE_CASCADE_PATH = None
    return _FACE_CASCADE


def _estimate_skin_ratio(frame) -> float:
    ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
    lower = (0, 133, 77)
    upper = (255, 173, 127)
    mask = cv2.inRange(ycrcb, lower, upper)
    return float((mask > 0).mean())


def _estimate_face_metrics(gray_frame) -> Dict[str, float]:
    detector = _get_face_cascade()
    if detector is None:
        return {"face_count": 0.0, "face_area_ratio": 0.0}
    h, w = gray_frame.shape[:2]
    if max(h, w) > 720:
        scale = 720.0 / max(h, w)
        resized = cv2.resize(gray_frame, (int(w * scale), int(h * scale)))
    else:
        scale = 1.0
        resized = gray_frame
    try:
        faces = detector.detectMultiScale(
            resized,
            scaleFactor=1.1,
            minNeighbors=4,
            minSize=(36, 36),
        )
    except cv2.error:
        faces = ()
    if len(faces) == 0:
        return {"face_count": 0.0, "face_area_ratio": 0.0}
    max_area = 0.0
    for (_, _, fw, fh) in faces:
        max_area = max(max_area, float(fw * fh) / (scale * scale))
    frame_area = float(h * w) if h > 0 and w > 0 else 1.0
    return {
        "face_count": float(len(faces)),
        "face_area_ratio": float(max_area / frame_area),
    }


def _frame_stats(frame) -> Dict[str, float]:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    face_metrics = _estimate_face_metrics(gray)
    skin_ratio = _estimate_skin_ratio(frame)
    brightness = float(gray.mean() / 255.0)
    contrast = float(gray.std() / 255.0)
    saturation = float(hsv[:, :, 1].mean() / 255.0)
    edges = cv2.Canny(gray, 80, 160)
    edge_density = float((edges > 0).mean())
    h, w = gray.shape[:2]
    center = gray[h // 4: (h * 3) // 4, w // 4: (w * 3) // 4]
    center_emphasis = float(center.mean() / 255.0) if center.size else brightness
    return {
        "brightness": round(brightness, 4),
        "contrast": round(contrast, 4),
        "saturation": round(saturation, 4),
        "edge_density": round(edge_density, 4),
        "skin_ratio": round(skin_ratio, 4),
        "face_count": int(face_metrics["face_count"]),
        "face_area_ratio": round(face_metrics["face_area_ratio"], 4),
        "center_emphasis": round(center_emphasis, 4),
    }


def _classify_visual_style(stats: Dict[str, float], motion_score: float) -> List[str]:
    tags: List[str] = []
    if motion_score >= 0.18:
        tags.append("动态镜头")
    elif motion_score <= 0.05:
        tags.append("静态镜头")
    if stats["brightness"] <= 0.28:
        tags.append("低照度场景")
    elif stats["brightness"] >= 0.68:
        tags.append("高亮场景")
    if stats["contrast"] >= 0.22:
        tags.append("细节对比明显")
    if stats["saturation"] >= 0.45:
        tags.append("色彩饱和")
    if stats["center_emphasis"] >= stats["brightness"] + 0.08:
        tags.append("主体居中")
    if stats["edge_density"] >= 0.14 and motion_score <= 0.10:
        tags.append("细节特写候选")
    return tags


def _classify_shot_scale(stats: Dict[str, float], motion_score: float, explicit_tags: List[str]) -> str:
    if "特写提示" in explicit_tags:
        return "特写镜头"
    if "近景提示" in explicit_tags:
        return "近景镜头"
    face_area_ratio = float(stats.get("face_area_ratio", 0.0))
    skin_ratio = float(stats.get("skin_ratio", 0.0))
    edge_density = float(stats.get("edge_density", 0.0))
    if face_area_ratio >= 0.12 or (skin_ratio >= 0.30 and edge_density <= 0.10):
        return "特写镜头"
    if face_area_ratio >= 0.04 or skin_ratio >= 0.18 or edge_density >= 0.14:
        return "近景镜头"
    if face_area_ratio >= 0.015 or motion_score >= 0.08:
        return "中景镜头"
    return "远景镜头"


def _classify_subject_type(role: str, stats: Dict[str, float], explicit_tags: List[str], motion_score: float) -> str:
    face_count = int(stats.get("face_count", 0))
    face_area_ratio = float(stats.get("face_area_ratio", 0.0))
    skin_ratio = float(stats.get("skin_ratio", 0.0))
    center_emphasis = float(stats.get("center_emphasis", 0.0))
    brightness = float(stats.get("brightness", 0.0))
    if face_count > 0 and face_area_ratio >= 0.018:
        return "人物主体"
    if "人物出镜" in explicit_tags or "口播讲解" in explicit_tags:
        return "人物主体"
    if skin_ratio >= 0.26:
        return "皮肤主体"
    if skin_ratio >= 0.12:
        return "手部主体"
    if "包装展示" in explicit_tags:
        return "包装主体"
    if role == "product" and center_emphasis >= brightness + 0.03 and motion_score <= 0.10:
        return "产品主体"
    if role == "symptom":
        return "皮肤主体"
    return "场景主体"


def _score_shot_purpose(
    role: str,
    explicit_tags: List[str],
    subject_type: str,
    shot_scale: str,
    motion_score: float,
    stats: Dict[str, float],
    description: str = "",
) -> Dict[str, float]:
    scores = {
        "讲解镜头": 0.05,
        "动作镜头": 0.05,
        "症状镜头": 0.05,
        "功效镜头": 0.05,
        "展示镜头": 0.05,
        "场景镜头": 0.05,
        "过渡镜头": 0.05,
    }
    desc = description or ""
    skin_ratio = float(stats.get("skin_ratio", 0.0))
    face_area_ratio = float(stats.get("face_area_ratio", 0.0))

    if subject_type == "人物主体" and (role == "speech" or "口播讲解" in explicit_tags):
        scores["讲解镜头"] += 0.72
    if role == "speech" and subject_type == "人物主体":
        scores["讲解镜头"] += 0.18

    if any(tag in explicit_tags for tag in ("抓挠动作", "涂抹动作", "喷雾动作", "手部动作")):
        scores["动作镜头"] += 0.72
    if subject_type == "手部主体" and motion_score >= 0.07:
        scores["动作镜头"] += 0.45
    if role == "product" and subject_type == "皮肤主体" and motion_score >= 0.08:
        scores["动作镜头"] += 0.30

    if role == "symptom" or any(tag in explicit_tags for tag in ("瘙痒困扰", "泛红表现", "患处展示")):
        scores["症状镜头"] += 0.70
    if subject_type == "皮肤主体" and role == "symptom":
        scores["症状镜头"] += 0.20

    if role == "product" and subject_type == "皮肤主体":
        if shot_scale in ("特写镜头", "近景镜头"):
            scores["功效镜头"] += 0.58
        if skin_ratio >= 0.22:
            scores["功效镜头"] += 0.18
        if motion_score <= 0.08:
            scores["功效镜头"] += 0.08

    if subject_type in ("包装主体", "产品主体") and motion_score <= 0.10:
        scores["展示镜头"] += 0.62
    if role == "product" and subject_type in ("包装主体", "产品主体", "场景主体") and shot_scale not in ("中景镜头", "远景镜头"):
        scores["展示镜头"] += 0.20

    if shot_scale in ("远景镜头", "中景镜头"):
        scores["场景镜头"] += 0.44
    if role == "product" and subject_type in ("包装主体", "产品主体", "场景主体") and shot_scale in ("中景镜头", "远景镜头"):
        scores["场景镜头"] += 0.22

    if desc:
        if any(token in desc for token in ("讲解", "口播", "对镜", "解说")):
            scores["讲解镜头"] += 0.25
        if any(token in desc for token in ("涂抹", "抓挠", "喷", "动作", "手部")):
            scores["动作镜头"] += 0.25
        if any(token in desc for token in ("红", "痒", "患处", "皮肤问题", "症状")):
            scores["症状镜头"] += 0.22
        if any(token in desc for token in ("修护", "舒缓", "改善", "恢复", "肤感")):
            scores["功效镜头"] += 0.22
        if any(token in desc for token in ("包装", "产品", "展示", "陈列", "外观")):
            scores["展示镜头"] += 0.22
        if any(token in desc for token in ("环境", "场景", "空间", "生活")):
            scores["场景镜头"] += 0.18

    if face_area_ratio > 0.12 and shot_scale in ("特写镜头", "近景镜头"):
        scores["讲解镜头"] += 0.12
    if subject_type == "皮肤主体" and motion_score <= 0.05 and shot_scale == "特写镜头":
        scores["功效镜头"] += 0.12
    if motion_score <= 0.04 and shot_scale in ("近景镜头", "特写镜头"):
        scores["过渡镜头"] += 0.08

    return {key: round(value, 4) for key, value in scores.items()}


def _resolve_shot_purpose(purpose_scores: Dict[str, float]) -> Tuple[str, float]:
    ordered = sorted(purpose_scores.items(), key=lambda item: item[1], reverse=True)
    if not ordered:
        return "过渡镜头", 0.0
    top_label, top_score = ordered[0]
    second_score = ordered[1][1] if len(ordered) > 1 else 0.0
    confidence = max(min(0.55 + (top_score - second_score) * 0.9 + min(top_score, 0.35), 0.98), 0.35)
    return top_label, round(confidence, 3)


def _build_shot_evidence(
    *,
    role: str,
    explicit_tags: List[str],
    stats: Dict[str, float],
    motion_score: float,
    subject_type: str,
    shot_scale: str,
    shot_purpose: str,
    purpose_scores: Dict[str, float],
) -> List[str]:
    evidence: List[str] = []
    if explicit_tags:
        evidence.append(f"路径线索: {' / '.join(explicit_tags[:3])}")
    if int(stats.get("face_count", 0)) > 0:
        evidence.append(f"检测到人脸 {int(stats.get('face_count', 0))} 个")
    if float(stats.get("skin_ratio", 0.0)) >= 0.12:
        evidence.append(f"肤色区域占比 {float(stats.get('skin_ratio', 0.0)):.2f}")
    if float(stats.get("face_area_ratio", 0.0)) >= 0.02:
        evidence.append(f"人脸面积占比 {float(stats.get('face_area_ratio', 0.0)):.2f}")
    evidence.append(f"运动强度 {motion_score:.3f}")
    evidence.append(
        f"亮度 {float(stats.get('brightness', 0.0)):.2f} / 对比度 {float(stats.get('contrast', 0.0)):.2f} / 边缘密度 {float(stats.get('edge_density', 0.0)):.2f}"
    )
    evidence.append(f"主体判断: {subject_type} | 景别判断: {shot_scale}")
    top_candidates = sorted(purpose_scores.items(), key=lambda item: item[1], reverse=True)[:2]
    evidence.append("用途候选: " + " / ".join(f"{name}:{score:.2f}" for name, score in top_candidates))
    evidence.append(f"角色上下文: {role} -> {shot_purpose}")
    return evidence[:8]


def _build_heuristic_description(
    *,
    role: str,
    explicit_tags: List[str],
    visual_tags: List[str],
    language_tags: List[str],
    duration_seconds: float,
) -> str:
    primary_tags = language_tags[:3] + explicit_tags[:1] + visual_tags[:1]
    primary = "、".join(primary_tags) if primary_tags else ROLE_CONTEXT_TAGS.get(role, ROLE_CONTEXT_TAGS["generic"])
    role_text = ROLE_DESCRIPTION_HINTS.get(role, ROLE_DESCRIPTION_HINTS["generic"])
    return f"{primary}，时长约 {duration_seconds:.1f} 秒，{role_text}。"


def _estimate_aes_score(
    *,
    subject_type: str,
    shot_scale: str,
    shot_purpose: str,
    purpose_confidence: float,
    motion_score: float,
    stats: Dict[str, float],
    description_source: str,
    duration_seconds: float,
) -> float:
    score = 0.52
    if 1.2 <= duration_seconds <= 4.5:
        score += 0.12
    elif duration_seconds > 0:
        score += 0.05
    if shot_scale in ("特写镜头", "近景镜头"):
        score += 0.08
    if shot_purpose in ("功效镜头", "展示镜头", "症状镜头", "动作镜头"):
        score += 0.09
    if subject_type in ("皮肤主体", "产品主体", "包装主体", "手部主体"):
        score += 0.08
    score += min(max(float(purpose_confidence or 0.0), 0.0), 1.0) * 0.12
    contrast = float(stats.get("contrast", 0.0))
    brightness = float(stats.get("brightness", 0.5))
    score += min(contrast, 0.28) * 0.25
    if 0.22 <= brightness <= 0.82:
        score += 0.05
    if 0.02 <= motion_score <= 0.18:
        score += 0.05
    if description_source == "vision_llm":
        score += 0.03
    return round(max(0.35, min(0.98, score)), 4)


def _merge_shot_tags(role: str, language_tags: List[str], explicit_tags: List[str], visual_tags: List[str]) -> List[str]:
    tags: List[str] = []
    for tag in language_tags + explicit_tags + visual_tags:
        if tag and tag not in tags:
            tags.append(tag)
    if not explicit_tags and not language_tags:
        fallback = ROLE_CONTEXT_TAGS.get(role, ROLE_CONTEXT_TAGS["generic"])
        if fallback and fallback not in tags:
            tags.insert(0, fallback)
    return tags[:8]


def _sample_frame_indices(start_frame: int, end_frame: int, sample_count: int = 3) -> List[int]:
    total = max(end_frame - start_frame, 1)
    if total <= sample_count:
        return [start_frame + idx for idx in range(total)]
    return [
        start_frame + int(total * ratio)
        for ratio in (0.2, 0.5, 0.8)[:sample_count]
    ]


def _detect_shot_boundaries(
    video_path: str,
    *,
    fps: float,
    frame_count: int,
    sample_fps: float,
    min_shot_duration: float,
    diff_threshold: float,
) -> List[Dict[str, int]]:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return [{"start_frame": 0, "end_frame": max(frame_count - 1, 0)}]

    sample_step = max(int(round(max(fps, 1.0) / max(sample_fps, 0.5))), 1)
    min_gap_frames = max(int(round(max(min_shot_duration, 0.5) * max(fps, 1.0))), sample_step)

    prev_hist = None
    prev_small = None
    last_cut_frame = 0
    current_frame = 0
    cuts: List[int] = [0]

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if current_frame % sample_step != 0:
                current_frame += 1
                continue

            small = cv2.resize(frame, (64, 36))
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
            cv2.normalize(hist, hist)

            if prev_hist is not None and prev_small is not None:
                hist_delta = float(cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA))
                gray_now = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                gray_prev = cv2.cvtColor(prev_small, cv2.COLOR_BGR2GRAY)
                pixel_delta = float(cv2.absdiff(gray_now, gray_prev).mean() / 255.0)
                score = hist_delta * 0.65 + pixel_delta * 0.35
                if score >= diff_threshold and (current_frame - last_cut_frame) >= min_gap_frames:
                    cuts.append(current_frame)
                    last_cut_frame = current_frame

            prev_hist = hist
            prev_small = small
            current_frame += 1
    finally:
        cap.release()

    if cuts[-1] != max(frame_count - 1, 0):
        cuts.append(max(frame_count - 1, 0))

    boundaries: List[Dict[str, int]] = []
    for idx in range(len(cuts) - 1):
        start_frame = cuts[idx]
        end_frame = max(cuts[idx + 1] - 1, start_frame)
        if boundaries and start_frame <= boundaries[-1]["end_frame"]:
            start_frame = boundaries[-1]["end_frame"] + 1
        if start_frame > end_frame:
            continue
        boundaries.append({"start_frame": start_frame, "end_frame": end_frame})

    if not boundaries:
        boundaries.append({"start_frame": 0, "end_frame": max(frame_count - 1, 0)})
    return boundaries


def _safe_video_fps(raw_fps: object, default: float = 25.0) -> float:
    try:
        fps = float(raw_fps)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(fps) or fps <= 0:
        return default
    return fps


def analyze_video_shots(
    video_path: str,
    *,
    role: str = "generic",
    use_vlm: bool = False,
    api_key: str = "",
    vlm_model: str = "",
    base_url: str = "",
    min_shot_duration: float = 0.8,
    diff_threshold: float = 0.42,
    sample_fps: float = 2.0,
    force_refresh: bool = False,
) -> Dict[str, object]:
    ensure_shot_output_root()
    fingerprint = _path_fingerprint(video_path)
    video_name = os.path.basename(video_path)
    safe_name = _safe_name(os.path.splitext(video_name)[0])
    cache_dir = os.path.join(SHOT_CACHE_ROOT, f"{safe_name}_{fingerprint}")
    thumbs_dir = os.path.join(cache_dir, "thumbnails")
    analysis_path = os.path.join(cache_dir, "analysis.json")
    os.makedirs(thumbs_dir, exist_ok=True)

    if not force_refresh:
        cached = _load_json(analysis_path)
        if cached and cached.get("source_path") == os.path.abspath(video_path):
            return cached

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"视频无法打开: {video_path}")

    fps = _safe_video_fps(cap.get(cv2.CAP_PROP_FPS), default=25.0)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration_seconds = frame_count / fps if fps > 0 else 0.0
    cap.release()

    boundaries = _detect_shot_boundaries(
        video_path,
        fps=fps,
        frame_count=frame_count,
        sample_fps=sample_fps,
        min_shot_duration=min_shot_duration,
        diff_threshold=diff_threshold,
    )

    explicit_tags = _infer_explicit_tags(video_path)
    cap = cv2.VideoCapture(video_path)
    shots: List[Dict[str, object]] = []
    vision_errors: List[str] = []
    try:
        for shot_index, boundary in enumerate(boundaries, start=1):
            start_frame = int(boundary["start_frame"])
            end_frame = int(boundary["end_frame"])
            mid_frame = start_frame + max((end_frame - start_frame) // 2, 0)
            cap.set(cv2.CAP_PROP_POS_FRAMES, mid_frame)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue

            thumb_path = os.path.join(thumbs_dir, f"shot_{shot_index:03d}.jpg")
            cv2.imwrite(thumb_path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])

            frame_indices = _sample_frame_indices(start_frame, end_frame, sample_count=3)
            motion_values: List[float] = []
            prev_gray = None
            for frame_idx in frame_indices:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                frame_ok, sample_frame = cap.read()
                if not frame_ok or sample_frame is None:
                    continue
                sample_gray = cv2.cvtColor(cv2.resize(sample_frame, (64, 36)), cv2.COLOR_BGR2GRAY)
                if prev_gray is not None:
                    motion_values.append(float(cv2.absdiff(sample_gray, prev_gray).mean() / 255.0))
                prev_gray = sample_gray

            motion_score = round(sum(motion_values) / len(motion_values), 4) if motion_values else 0.0
            stats = _frame_stats(frame)
            visual_tags = _classify_visual_style(stats, motion_score)
            shot_scale = _classify_shot_scale(stats, motion_score, explicit_tags)
            subject_type = _classify_subject_type(role, stats, explicit_tags, motion_score)
            purpose_scores = _score_shot_purpose(
                role,
                explicit_tags,
                subject_type,
                shot_scale,
                motion_score,
                stats,
            )
            shot_purpose, purpose_confidence = _resolve_shot_purpose(purpose_scores)
            language_tags = [subject_type, shot_scale, shot_purpose]
            shot_duration = max((end_frame - start_frame + 1) / max(fps, 1.0), 0.04)

            description = _build_heuristic_description(
                role=role,
                explicit_tags=explicit_tags,
                visual_tags=visual_tags,
                language_tags=language_tags,
                duration_seconds=shot_duration,
            )
            description_source = "heuristic"
            if use_vlm and api_key and vlm_model:
                prompt = (
                    "你是视频镜头标注助手。请只根据这张关键帧，输出一段简洁中文描述，"
                    "重点包含主体、动作、场景、镜头用途，控制在 40 字以内。"
                )
                try:
                    vlm_description = describe_image_with_llm(
                        api_key=api_key,
                        model=vlm_model,
                        base_url=base_url,
                        prompt=prompt,
                        image_path=thumb_path,
                        max_tokens=120,
                    )
                    if vlm_description:
                        description = vlm_description.strip()
                        description_source = "vision_llm"
                        purpose_scores = _score_shot_purpose(
                            role,
                            explicit_tags,
                            subject_type,
                            shot_scale,
                            motion_score,
                            stats,
                            description=description,
                        )
                        shot_purpose, purpose_confidence = _resolve_shot_purpose(purpose_scores)
                        language_tags = [subject_type, shot_scale, shot_purpose]
                except Exception as exc:
                    vision_errors.append(f"{video_name} 镜头 {shot_index}: {exc}")

            aes_score = _estimate_aes_score(
                subject_type=subject_type,
                shot_scale=shot_scale,
                shot_purpose=shot_purpose,
                purpose_confidence=purpose_confidence,
                motion_score=motion_score,
                stats=stats,
                description_source=description_source,
                duration_seconds=shot_duration,
            )
            tags = _merge_shot_tags(role, language_tags, explicit_tags, visual_tags)
            evidence = _build_shot_evidence(
                role=role,
                explicit_tags=explicit_tags,
                stats=stats,
                motion_score=motion_score,
                subject_type=subject_type,
                shot_scale=shot_scale,
                shot_purpose=shot_purpose,
                purpose_scores=purpose_scores,
            )

            shots.append({
                "shot_index": shot_index,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "start_seconds": round(start_frame / max(fps, 1.0), 3),
                "end_seconds": round((end_frame + 1) / max(fps, 1.0), 3),
                "duration_seconds": round(shot_duration, 3),
                "thumbnail_path": thumb_path,
                "caption": description,
                "description": description,
                "description_source": description_source,
                "aes_score": aes_score,
                "tags": tags[:8],
                "shot_scale": shot_scale,
                "subject_type": subject_type,
                "shot_purpose": shot_purpose,
                "shot_purpose_confidence": purpose_confidence,
                "shot_purpose_candidates": purpose_scores,
                "evidence": evidence,
                "explicit_tags": explicit_tags[:6],
                "role_context": ROLE_CONTEXT_TAGS.get(role, ROLE_CONTEXT_TAGS["generic"]),
                "motion_score": motion_score,
                "brightness": stats["brightness"],
                "contrast": stats["contrast"],
                "skin_ratio": stats["skin_ratio"],
                "face_count": stats["face_count"],
                "face_area_ratio": stats["face_area_ratio"],
            })
    finally:
        cap.release()

    if not shots:
        raise RuntimeError(f"未能从视频中提取可用镜头: {video_path}")

    payload = {
        "source_path": os.path.abspath(video_path),
        "source_name": video_name,
        "role": role,
        "fingerprint": fingerprint,
        "cache_dir": cache_dir,
        "analysis_path": analysis_path,
        "duration_seconds": round(duration_seconds, 3),
        "fps": round(fps, 3),
        "frame_count": frame_count,
        "shot_count": len(shots),
        "use_vlm": bool(use_vlm and api_key and vlm_model),
        "vlm_model": vlm_model if use_vlm else "",
        "vision_errors": vision_errors,
        "shots": shots,
        "written_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_json(analysis_path, payload)
    return payload


def analyze_directory(
    source_dir: str,
    *,
    role: str = "generic",
    recursive: bool = True,
    max_videos: int = 8,
    use_vlm: bool = False,
    api_key: str = "",
    vlm_model: str = "",
    base_url: str = "",
    min_shot_duration: float = 0.8,
    diff_threshold: float = 0.42,
    sample_fps: float = 2.0,
    force_refresh: bool = False,
) -> Dict[str, object]:
    ensure_shot_output_root()
    source_dir = os.path.abspath(source_dir)
    video_paths, skipped_audio = scan_video_file_paths(
        source_dir,
        recursive=recursive,
        skip_generated_artifacts=True,
    )
    selected_paths = video_paths[: max(max_videos, 1)]
    videos: List[Dict[str, object]] = []
    total_shots = 0
    errors: List[Dict[str, str]] = []
    warnings: List[str] = []

    if use_vlm and (not api_key or not vlm_model):
        warnings.append("已勾选大模型镜头描述，但未填写可用 API Key 或视觉模型名，已回退为启发式描述。")
        use_vlm = False

    for video_path in selected_paths:
        try:
            video_report = analyze_video_shots(
                video_path,
                role=role,
                use_vlm=use_vlm,
                api_key=api_key,
                vlm_model=vlm_model,
                base_url=base_url,
                min_shot_duration=min_shot_duration,
                diff_threshold=diff_threshold,
                sample_fps=sample_fps,
                force_refresh=force_refresh,
            )
            videos.append(video_report)
            total_shots += int(video_report.get("shot_count", 0))
            for item in video_report.get("vision_errors", []):
                warnings.append(item)
        except Exception as exc:
            errors.append({
                "video_path": video_path,
                "error": str(exc),
            })

    report = {
        "source_dir": source_dir,
        "role": role,
        "video_count": len(videos),
        "shot_count": total_shots,
        "requested_max_videos": max_videos,
        "processed_videos": [item.get("source_path") for item in videos],
        "skipped_audio_count": len(skipped_audio),
        "errors": errors,
        "warnings": warnings,
        "videos": videos,
        "latest_report_path": LATEST_SHOT_REPORT_PATH,
        "written_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_json(LATEST_SHOT_REPORT_PATH, report)
    return report


def load_latest_report() -> Optional[Dict[str, object]]:
    return _load_json(LATEST_SHOT_REPORT_PATH)
