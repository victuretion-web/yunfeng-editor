import json
import math
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple


PRIMARY_SEMANTIC_THRESHOLD = 0.75
CASCADE_REUSE_THRESHOLD = 0.60
EMERGENCY_GENERIC_RATIO = 0.20
WINDOW_TARGET_RATIO = 0.65
WINDOW_SECONDS = 15 * 60
SEMANTIC_ANCHORS = {
    "symptom": "__sem_symptom__",
    "product": "__sem_product__",
    "generic": "__sem_generic__",
}

TOKEN_ALIASES = {
    "prod": ["产品", "__sem_product__"],
    "product": ["产品", "__sem_product__"],
    "cream": ["乳膏", "涂抹", "__sem_product__"],
    "serum": ["精华", "成分", "__sem_product__"],
    "spray": ["喷雾", "喷剂", "__sem_product__"],
    "repair": ["修护", "改善", "__sem_product__"],
    "daily": ["日常", "温和", "__sem_product__"],
    "symptom": ["症状", "__sem_symptom__"],
    "itch": ["瘙痒", "不适", "__sem_symptom__"],
    "redness": ["泛红", "红斑", "__sem_symptom__"],
    "flakes": ["脱屑", "干燥", "__sem_symptom__"],
    "pain": ["疼痛", "刺激", "__sem_symptom__"],
    "skin": ["皮肤", "困扰", "__sem_symptom__"],
}

SEMANTIC_KEYWORDS = {
    "symptom": [
        "症状", "表现", "困扰", "瘙痒", "疼痛", "不适", "红斑", "脱屑",
        "皮肤", "感染", "真菌", "体癣", "股癣", "手足癣", "难受", "影响",
        "生活质量", "睡眠", "工作", "社交", "尴尬", "反复", "泛红", "红肿",
    ],
    "product": [
        "产品", "治疗", "使用", "方法", "效果", "改善", "推荐", "购买",
        "我们的", "这款", "这个", "成分", "功效", "特点", "优势", "安全",
        "无刺激", "温和", "快速", "有效", "专业", "认证", "批准",
        "胶囊", "乳膏", "喷雾", "软膏", "药膏", "抑菌", "止痒", "涂抹",
        "疗程", "外用", "口服", "达克宁", "百癣夏塔热",
    ],
    "generic": [
        "通用", "泛用", "展示", "细节", "局部", "质地", "包装", "场景",
        "镜头", "特写", "演示", "说明", "氛围", "空镜",
    ],
}


def _normalize_text(text: str) -> str:
    text = str(text or "").lower()
    text = re.sub(r"[\[\]【】()（）_/\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_chinese_ngrams(text: str, n: int = 2) -> List[str]:
    han = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    if len(han) < n:
        return [han] if han else []
    return [han[idx: idx + n] for idx in range(len(han) - n + 1)]


def _extract_semantic_keywords(text: str) -> Dict[str, List[str]]:
    hits: Dict[str, List[str]] = {"symptom": [], "product": [], "generic": []}
    for semantic_type, words in SEMANTIC_KEYWORDS.items():
        for word in words:
            if word in text:
                hits[semantic_type].append(word)
    return hits


def infer_semantic_type(text: str, fallback: str = "generic") -> str:
    normalized = _normalize_text(text)
    hits = _extract_semantic_keywords(normalized)
    symptom_score = len(hits["symptom"])
    product_score = len(hits["product"])
    generic_score = len(hits["generic"])
    if symptom_score > product_score and symptom_score > 0:
        return "symptom"
    if product_score > symptom_score and product_score > 0:
        return "product"
    if generic_score > 0:
        return "generic"
    return fallback


def build_semantic_profile(
    text: str,
    semantic_type: str = "",
    tags: Optional[Sequence[str]] = None,
    emotion_strength: str = "medium",
    is_generic: bool = False,
) -> Dict[str, object]:
    normalized = _normalize_text(text)
    inferred_type = semantic_type or infer_semantic_type(normalized)
    weights: Counter = Counter()
    keyword_hits = _extract_semantic_keywords(normalized)
    semantic_anchor = SEMANTIC_ANCHORS.get(inferred_type, SEMANTIC_ANCHORS["generic"])

    for token in re.findall(r"[a-z0-9]{2,}", normalized):
        weights[token] += 1.0
        for alias in TOKEN_ALIASES.get(token, []):
            weights[alias] += 1.4 if alias.startswith("__sem_") else 1.0
    for token in _extract_chinese_ngrams(normalized, n=2):
        weights[token] += 0.35
    for token in _extract_chinese_ngrams(normalized, n=3):
        weights[token] += 0.2
    weights[semantic_anchor] += 3.0
    for semantic_tokens in keyword_hits.values():
        for token in semantic_tokens:
            weights[token] += 2.2
    for tag in tags or []:
        tag_norm = _normalize_text(tag)
        if not tag_norm:
            continue
        weights[tag_norm] += 1.6
        for alias in TOKEN_ALIASES.get(tag_norm, []):
            weights[alias] += 1.5 if alias.startswith("__sem_") else 1.1
        for token in _extract_chinese_ngrams(tag_norm, n=2):
            weights[token] += 0.5

    return {
        "semantic_type": inferred_type,
        "emotion_strength": emotion_strength or "medium",
        "token_weights": dict(weights),
        "keyword_hits": keyword_hits,
        "text": normalized,
        "is_generic": bool(is_generic),
    }


def weighted_jaccard(left: Dict[str, float], right: Dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    keys = set(left) | set(right)
    numerator = 0.0
    denominator = 0.0
    for key in keys:
        lv = float(left.get(key, 0.0))
        rv = float(right.get(key, 0.0))
        numerator += min(lv, rv)
        denominator += max(lv, rv)
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def compute_semantic_similarity(candidate_profile: Dict[str, object], material_profile: Dict[str, object]) -> float:
    token_score = weighted_jaccard(
        candidate_profile.get("token_weights", {}),
        material_profile.get("token_weights", {}),
    )
    candidate_type = candidate_profile.get("semantic_type", "generic")
    material_type = material_profile.get("semantic_type", "generic")
    candidate_anchor = SEMANTIC_ANCHORS.get(candidate_type, SEMANTIC_ANCHORS["generic"])
    material_tokens = material_profile.get("token_weights", {})
    anchor_score = 1.0 if candidate_anchor in material_tokens else 0.0
    type_bonus = 0.0
    if candidate_type == material_type:
        type_bonus = 0.18
    elif candidate_type in ("symptom", "product") and material_type in ("symptom", "product"):
        type_bonus = -0.22
    else:
        type_bonus = 0.0

    emotion_bonus = 0.04 if candidate_profile.get("emotion_strength") == material_profile.get("emotion_strength") else 0.0
    generic_penalty = 0.05 if material_profile.get("is_generic") else 0.0
    score = (anchor_score * 0.55) + (token_score * 0.25) + type_bonus + emotion_bonus - generic_penalty
    return max(0.0, min(1.0, round(score, 4)))


def is_generic_material(material: Dict[str, object]) -> bool:
    material_type = _resolve_material_semantic_type(material)
    if material_type in ("product", "symptom"):
        return False
    combined = " ".join(
        [
            str(material.get("filename", "")),
            " ".join(material.get("tags", []) or []),
            str(material.get("type", "")),
        ]
    )
    inferred = infer_semantic_type(combined, fallback="generic")
    return inferred == "generic"


def _resolve_material_semantic_type(material: Dict[str, object]) -> str:
    # 来源文件夹标签为最高优先级，不可被关键词推断覆盖
    source = str(material.get("_source_type", "")).strip()
    if source in ("product", "symptom", "generic"):
        return source

    explicit = str(material.get("semantic_type", "")).strip()
    if explicit in ("product", "symptom", "generic"):
        return explicit
    material_group = str(material.get("type", "")).strip().lower()
    if material_group in ("产品", "product", "prod"):
        return "product"
    if material_group in ("病症", "symptom"):
        return "symptom"
    combined = " ".join(
        [
            str(material.get("filename", "")),
            " ".join(material.get("tags", []) or []),
            material_group,
        ]
    )
    return infer_semantic_type(combined, fallback="generic")


def build_material_semantic_library(videos: Sequence[Dict[str, object]], emergency_ratio: float = EMERGENCY_GENERIC_RATIO) -> Dict[str, object]:
    materials: List[Dict[str, object]] = []
    generic_materials: List[Dict[str, object]] = []

    for video in videos:
        combined_text = " ".join(
            [
                str(video.get("filename", "")),
                " ".join(video.get("tags", []) or []),
                str(video.get("type", "")),
            ]
        )
        semantic_type = _resolve_material_semantic_type(video)
        generic = is_generic_material(video)
        enriched = dict(video)
        enriched["semantic_type"] = semantic_type
        enriched["is_emergency_generic"] = generic
        enriched["semantic_profile"] = build_semantic_profile(
            text=combined_text,
            semantic_type=semantic_type,
            tags=video.get("tags", []) or [],
            emotion_strength=str(video.get("emotion_strength", "medium")),
            is_generic=generic,
        )
        materials.append(enriched)
        if generic:
            generic_materials.append(enriched)

    emergency_limit = int(math.ceil(len(materials) * max(0.0, emergency_ratio))) if materials else 0
    if emergency_limit < 1 and emergency_ratio > 0:
        emergency_limit = 1
    emergency_pool = generic_materials[:emergency_limit]
    return {"materials": materials, "emergency_pool": emergency_pool}


def rank_materials_by_semantics(candidate: Dict[str, object], materials: Sequence[Dict[str, object]]) -> List[Tuple[float, Dict[str, object]]]:
    candidate_profile = build_semantic_profile(
        text=str(candidate.get("text", "")),
        semantic_type=str(candidate.get("semantic_type", "")),
        tags=[],
        emotion_strength=str(candidate.get("emotion_strength", "medium")),
        is_generic=False,
    )
    ranked: List[Tuple[float, Dict[str, object]]] = []
    for material in materials:
        material_profile = material.get("semantic_profile") or build_semantic_profile(
            text=str(material.get("filename", "")),
            semantic_type=str(material.get("semantic_type", "")),
            tags=material.get("tags", []) or [],
            emotion_strength=str(material.get("emotion_strength", "medium")),
            is_generic=bool(material.get("is_emergency_generic")),
        )
        score = compute_semantic_similarity(candidate_profile, material_profile)
        ranked.append((score, material))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


class BrollUsageHistory:
    def __init__(self, history_path: str, cooldown_hours: int = 24):
        self.history_path = history_path
        self.cooldown_hours = max(1, int(cooldown_hours))
        self.entries: List[Dict[str, object]] = []
        self._load()

    def _load(self) -> None:
        if not self.history_path or not os.path.exists(self.history_path):
            self.entries = []
            return
        try:
            with open(self.history_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            self.entries = list(payload.get("entries", []))
        except Exception:
            self.entries = []
        self.prune()

    def prune(self) -> None:
        if not self.entries:
            return
        cutoff = datetime.now() - timedelta(hours=self.cooldown_hours)
        kept = []
        for entry in self.entries:
            try:
                used_at = datetime.fromisoformat(str(entry.get("used_at")))
            except Exception:
                continue
            if used_at >= cutoff:
                kept.append(entry)
        self.entries = kept

    def save(self) -> None:
        if not self.history_path:
            return
        os.makedirs(os.path.dirname(self.history_path), exist_ok=True)
        self.prune()
        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump({"entries": self.entries}, f, ensure_ascii=False, indent=2)

    def is_recently_used(self, content_hash: str) -> bool:
        self.prune()
        target = str(content_hash or "").strip()
        if not target:
            return False
        return any(str(entry.get("content_hash", "")) == target for entry in self.entries)

    def record_use(
        self,
        video_id: str,
        material: Dict[str, object],
        semantic_score: float,
        fallback_level: str,
    ) -> None:
        self.entries.append(
            {
                "video_id": video_id,
                "content_hash": material.get("content_hash", ""),
                "unique_id": material.get("unique_id", ""),
                "path": material.get("path", ""),
                "filename": material.get("filename", ""),
                "semantic_type": material.get("semantic_type", ""),
                "semantic_score": round(float(semantic_score), 4),
                "fallback_level": fallback_level,
                "used_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        self.save()

    def get_previous_video_materials(
        self,
        current_video_id: str,
        all_materials: Sequence[Dict[str, object]],
    ) -> List[Dict[str, object]]:
        self.prune()
        material_map = {
            str(item.get("content_hash", "")): item
            for item in all_materials
            if item.get("content_hash")
        }
        previous_video_id = None
        for entry in reversed(self.entries):
            entry_video_id = str(entry.get("video_id", "")).strip()
            if not entry_video_id or entry_video_id == current_video_id:
                continue
            previous_video_id = entry_video_id
            break
        if not previous_video_id:
            return []

        results: List[Dict[str, object]] = []
        seen_hashes = set()
        for entry in reversed(self.entries):
            if entry.get("video_id") != previous_video_id:
                continue
            content_hash = str(entry.get("content_hash", ""))
            if not content_hash or content_hash in seen_hashes:
                continue
            material = material_map.get(content_hash)
            if material:
                results.append(material)
                seen_hashes.add(content_hash)
        return results


def analyze_broll_ratio(
    matches: Sequence[Dict[str, object]],
    video_duration: float,
    target_ratio: float = WINDOW_TARGET_RATIO,
    window_seconds: int = WINDOW_SECONDS,
) -> Dict[str, object]:
    total_insert = sum(float(item.get("duration", 0.0)) for item in matches)
    total_ratio = (total_insert / video_duration) if video_duration > 0 else 0.0
    windows = []
    if video_duration > 0:
        total_windows = max(1, int(math.ceil(video_duration / float(window_seconds))))
        for idx in range(total_windows):
            start = idx * float(window_seconds)
            end = min(video_duration, start + float(window_seconds))
            covered = 0.0
            for match in matches:
                covered += max(
                    0.0,
                    min(float(match.get("end_time", 0.0)), end) - max(float(match.get("start_time", 0.0)), start),
                )
            duration = max(0.001, end - start)
            ratio = covered / duration
            windows.append(
                {
                    "window_index": idx + 1,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "covered_duration": round(covered, 3),
                    "required_duration": round(duration * target_ratio, 3),
                    "ratio": round(ratio, 4),
                    "status": "达标" if ratio >= target_ratio else "待补齐",
                }
            )
    return {
        "target_ratio": target_ratio,
        "total_insert_duration": round(total_insert, 3),
        "total_ratio": round(total_ratio, 4),
        "total_status": "达标" if total_ratio >= target_ratio else "待补齐",
        "windows": windows,
    }
