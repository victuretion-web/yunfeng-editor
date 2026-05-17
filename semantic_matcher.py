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
GENERIC_SCENE = "generic_scene"
GENERIC_ACTION = "generic_action"

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

INTENT_PATTERNS: Dict[str, Dict[str, object]] = {
    "symptom_appearance": {"semantic_type": "symptom", "keywords": ["红斑", "泛红", "脱屑", "丘疹", "皮损", "斑块", "水泡", "起皮", "粗糙"]},
    "symptom_feeling": {"semantic_type": "symptom", "keywords": ["瘙痒", "刺痛", "灼热", "难受", "疼", "痒", "发痒", "不舒服"]},
    "symptom_scenario": {"semantic_type": "symptom", "keywords": ["晚上", "夜里", "白天", "出门", "工作", "睡觉", "社交", "反复", "影响生活", "尴尬"]},
    "product_form": {"semantic_type": "product", "keywords": ["包装", "膏体", "喷雾", "乳膏", "软膏", "外盒", "瓶身", "质地"]},
    "product_usage": {"semantic_type": "product", "keywords": ["涂抹", "喷", "使用", "外用", "按说明", "清洗后", "均匀涂开", "坚持使用"]},
    "product_effect": {"semantic_type": "product", "keywords": ["改善", "缓解", "抑菌", "止痒", "恢复", "修护", "见效", "效果"]},
    "trust_or_authority": {"semantic_type": "product", "keywords": ["专业", "医生", "药监", "认证", "批准", "成分", "安全", "温和"]},
    "warning_or_taboo": {"semantic_type": "product", "keywords": ["注意", "禁忌", "避免", "不要", "慎用", "遵医嘱"]},
    "generic_transition": {"semantic_type": "generic", "keywords": ["同时", "另外", "接下来", "再看", "过渡", "然后"]},
}

ACTION_PATTERNS = {
    "scratch_relief": ["抓", "抓挠", "搔抓", "挠"],
    "apply_ointment": ["涂抹", "抹开", "均匀涂", "外用"],
    "spray_application": ["喷", "喷雾", "喷上"],
    "packshot_display": ["包装", "外盒", "瓶身", "质地", "展示"],
    "skin_closeup": ["局部", "患处", "皮肤", "特写", "近景"],
    "daily_life_scene": ["睡觉", "工作", "出门", "社交", "走路", "洗澡"],
}

SCENE_PATTERNS = {
    "night_home": ["晚上", "夜里", "夜间", "睡觉", "卧室", "床上"],
    "daily_home": ["家里", "洗澡", "起床", "居家"],
    "work_social": ["工作", "上班", "开会", "社交", "出门", "约会"],
    "bathroom_usage": ["洗澡", "清洗", "浴室", "镜子"],
    "clinical_or_lab": ["医生", "专业", "实验", "检测", "认证"],
}

CAMERA_SHOT_PATTERNS = {
    "closeup": ["特写", "局部", "近景", "细节"],
    "medium": ["中景", "半身", "演示"],
    "wide": ["全景", "场景", "远景", "环境"],
}

MOTION_LEVEL_PATTERNS = {
    "high": ["快切", "动态", "动作", "抓挠", "涂抹", "喷涂"],
    "medium": ["演示", "移动", "转动", "对比"],
    "low": ["静物", "包装", "空镜", "摆拍"],
}

ENTITY_VOCABULARY = {
    "瘙痒": ["瘙痒", "发痒", "痒"],
    "泛红": ["泛红", "红斑", "红肿", "发红"],
    "脱屑": ["脱屑", "掉皮", "起皮", "鳞屑"],
    "疼痛": ["疼痛", "刺痛", "灼热"],
    "睡眠": ["睡觉", "夜里", "晚上", "睡眠"],
    "社交": ["社交", "尴尬", "出门", "见人"],
    "乳膏": ["乳膏", "软膏", "药膏"],
    "喷雾": ["喷雾", "喷剂", "喷上"],
    "成分": ["成分", "抑菌", "修护", "安全"],
    "涂抹": ["涂抹", "均匀涂开", "外用"],
}


def _normalize_text(text: str) -> str:
    text = str(text or "").lower()
    text = re.sub(r"[\[\]【】()（）_/\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_label(value: str, fallback: str = "") -> str:
    return _normalize_text(value).replace(" ", "_") or fallback


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


def _first_pattern_hit(text: str, patterns: Dict[str, List[str]], fallback: str) -> str:
    for label, keywords in patterns.items():
        if any(keyword in text for keyword in keywords):
            return label
    return fallback


def _extract_entities(text: str, semantic_type: str, tags: Optional[Sequence[str]] = None) -> List[str]:
    combined = " ".join([text] + list(tags or []))
    entities: List[str] = []
    for entity, keywords in ENTITY_VOCABULARY.items():
        if any(keyword in combined for keyword in keywords):
            entities.append(entity)
    if not entities:
        keywords = _extract_semantic_keywords(text)
        fallback_entities = keywords.get(semantic_type, [])[:4]
        entities.extend(fallback_entities)
    return sorted(dict.fromkeys(entities))


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


def infer_intent(text: str, semantic_type: str = "") -> str:
    normalized = _normalize_text(text)
    inferred_type = semantic_type or infer_semantic_type(normalized)
    best_intent = "generic_transition" if inferred_type == "generic" else f"{inferred_type}_feeling"
    best_score = -1
    for intent, payload in INTENT_PATTERNS.items():
        if payload.get("semantic_type") not in (inferred_type, "generic"):
            continue
        score = sum(1 for keyword in payload.get("keywords", []) if keyword in normalized)
        if score > best_score:
            best_intent = intent
            best_score = score
    if best_score <= 0:
        if inferred_type == "product":
            return "product_usage"
        if inferred_type == "symptom":
            return "symptom_feeling"
        return "generic_transition"
    return best_intent


def infer_action_type(text: str, semantic_type: str = "", intent: str = "") -> str:
    normalized = _normalize_text(text)
    action = _first_pattern_hit(normalized, ACTION_PATTERNS, GENERIC_ACTION)
    if action != GENERIC_ACTION:
        return action
    if intent == "product_usage":
        return "apply_ointment" if semantic_type == "product" else GENERIC_ACTION
    if intent == "product_form":
        return "packshot_display"
    if intent.startswith("symptom_"):
        return "skin_closeup"
    return GENERIC_ACTION


def infer_scene_hint(text: str) -> str:
    return _first_pattern_hit(_normalize_text(text), SCENE_PATTERNS, GENERIC_SCENE)


def infer_camera_shot(text: str, tags: Optional[Sequence[str]] = None) -> str:
    combined = _normalize_text(" ".join([text] + list(tags or [])))
    return _first_pattern_hit(combined, CAMERA_SHOT_PATTERNS, "medium")


def infer_motion_level(text: str, tags: Optional[Sequence[str]] = None) -> str:
    combined = _normalize_text(" ".join([text] + list(tags or [])))
    return _first_pattern_hit(combined, MOTION_LEVEL_PATTERNS, "medium")


def infer_visual_need(intent: str, action_type: str) -> str:
    if action_type == "scratch_relief":
        return "scratch_relief"
    if action_type in ("apply_ointment", "spray_application"):
        return "usage_demonstration"
    if intent == "product_effect":
        return "effect_transition"
    if intent.startswith("symptom_"):
        return "symptom_highlight"
    return "generic_support"


def build_segment_context_window(text: str, context_prev: str = "", context_next: str = "") -> str:
    parts = [str(context_prev or "").strip(), str(text or "").strip(), str(context_next or "").strip()]
    return " | ".join(part for part in parts if part)


def _build_quality_score(material: Dict[str, object], tags: Optional[Sequence[str]] = None) -> float:
    score = 0.68
    duration = float(material.get("duration", 0.0))
    if 2.0 <= duration <= 6.5:
        score += 0.12
    if bool(material.get("is_vertical")):
        score += 0.08
    if len(list(tags or [])) >= 2:
        score += 0.05
    shot = infer_camera_shot(str(material.get("filename", "")), tags=tags)
    if shot == "closeup":
        score += 0.03
    return max(0.45, min(0.98, round(score, 4)))


def build_semantic_profile(
    text: str,
    semantic_type: str = "",
    tags: Optional[Sequence[str]] = None,
    emotion_strength: str = "medium",
    is_generic: bool = False,
    context_prev: str = "",
    context_next: str = "",
    intent: str = "",
    action_type: str = "",
    scene_hint: str = "",
    entities: Optional[Sequence[str]] = None,
    camera_shot: str = "",
    motion_level: str = "",
    visual_quality: Optional[float] = None,
) -> Dict[str, object]:
    normalized = _normalize_text(text)
    inferred_type = semantic_type or infer_semantic_type(normalized)
    inferred_intent = intent or infer_intent(normalized, inferred_type)
    inferred_action = action_type or infer_action_type(normalized, inferred_type, inferred_intent)
    inferred_scene = scene_hint or infer_scene_hint(normalized)
    inferred_entities = list(entities or _extract_entities(normalized, inferred_type, tags=tags))
    inferred_camera_shot = camera_shot or infer_camera_shot(normalized, tags=tags)
    inferred_motion = motion_level or infer_motion_level(normalized, tags=tags)
    context_text = _normalize_text(build_segment_context_window(normalized, context_prev, context_next))
    visual_need = infer_visual_need(inferred_intent, inferred_action)
    weights: Counter = Counter()
    keyword_hits = _extract_semantic_keywords(normalized)
    semantic_anchor = SEMANTIC_ANCHORS.get(inferred_type, SEMANTIC_ANCHORS["generic"])

    base_tokens = re.findall(r"[a-z0-9]{2,}", normalized)
    base_tokens.extend(re.findall(r"[a-z0-9_]{3,}", context_text))
    for token in base_tokens:
        weights[token] += 1.0
        for alias in TOKEN_ALIASES.get(token, []):
            weights[alias] += 1.4 if alias.startswith("__sem_") else 1.0
    for token in _extract_chinese_ngrams(normalized, n=2):
        weights[token] += 0.35
    for token in _extract_chinese_ngrams(normalized, n=3):
        weights[token] += 0.2
    for token in _extract_chinese_ngrams(context_text, n=2):
        weights[token] += 0.18
    weights[semantic_anchor] += 3.0
    weights[f"intent:{inferred_intent}"] += 2.8
    weights[f"action:{inferred_action}"] += 2.2
    weights[f"scene:{inferred_scene}"] += 1.7
    weights[f"need:{visual_need}"] += 1.2
    weights[f"shot:{inferred_camera_shot}"] += 0.8
    weights[f"motion:{inferred_motion}"] += 0.8
    for entity in inferred_entities:
        weights[f"entity:{entity}"] += 1.6
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
        "intent": inferred_intent,
        "action_type": inferred_action,
        "scene_hint": inferred_scene,
        "entities": inferred_entities,
        "visual_need": visual_need,
        "camera_shot": inferred_camera_shot,
        "motion_level": inferred_motion,
        "emotion_strength": emotion_strength or "medium",
        "token_weights": dict(weights),
        "keyword_hits": keyword_hits,
        "text": normalized,
        "context_text": context_text,
        "is_generic": bool(is_generic),
        "visual_quality": float(visual_quality if visual_quality is not None else 0.75),
    }


def build_segment_semantic_script(
    text: str,
    semantic_type: str = "",
    tags: Optional[Sequence[str]] = None,
    emotion_strength: str = "medium",
    context_prev: str = "",
    context_next: str = "",
    trigger_reason: str = "",
    intent: str = "",
    action_type: str = "",
    scene_hint: str = "",
    entities: Optional[Sequence[str]] = None,
) -> Dict[str, object]:
    profile = build_semantic_profile(
        text=text,
        semantic_type=semantic_type,
        tags=tags,
        emotion_strength=emotion_strength,
        is_generic=False,
        context_prev=context_prev,
        context_next=context_next,
        intent=intent,
        action_type=action_type,
        scene_hint=scene_hint,
        entities=entities,
    )
    profile["trigger_reason"] = trigger_reason or "semantic"
    return profile


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


def _semantic_prefix(value: str) -> str:
    head = str(value or "").split("_", 1)[0]
    return head or "generic"


def _exact_or_prefix_score(left: str, right: str, fallback: float = 0.0, generic_score: float = 0.42) -> float:
    if not left or not right:
        return generic_score if (left == right == "") else fallback
    if left == right:
        return 1.0
    if left == "generic" or right == "generic" or left == GENERIC_ACTION or right == GENERIC_ACTION or left == GENERIC_SCENE or right == GENERIC_SCENE:
        return generic_score
    if _semantic_prefix(left) == _semantic_prefix(right):
        return 0.7
    return fallback


def _entity_overlap_score(left: Sequence[str], right: Sequence[str]) -> float:
    left_set = {str(item).strip() for item in left or [] if str(item).strip()}
    right_set = {str(item).strip() for item in right or [] if str(item).strip()}
    if not left_set or not right_set:
        return 0.25
    overlap = len(left_set & right_set)
    union = len(left_set | right_set)
    return overlap / union if union else 0.0


def compute_semantic_match_details(candidate_profile: Dict[str, object], material_profile: Dict[str, object]) -> Dict[str, float]:
    token_score = weighted_jaccard(
        candidate_profile.get("token_weights", {}),
        material_profile.get("token_weights", {}),
    )
    candidate_type = str(candidate_profile.get("semantic_type", "generic"))
    material_type = str(material_profile.get("semantic_type", "generic"))
    candidate_anchor = SEMANTIC_ANCHORS.get(candidate_type, SEMANTIC_ANCHORS["generic"])
    material_tokens = material_profile.get("token_weights", {})
    anchor_score = 1.0 if candidate_anchor in material_tokens else 0.0
    type_alignment = 1.0 if candidate_type == material_type else (0.12 if "generic" in (candidate_type, material_type) else 0.0)
    intent_score = _exact_or_prefix_score(
        str(candidate_profile.get("intent", "")),
        str(material_profile.get("intent", "")),
        fallback=0.2 if candidate_type == material_type else 0.0,
        generic_score=0.45,
    )
    entity_score = _entity_overlap_score(
        candidate_profile.get("entities", []) or [],
        material_profile.get("entities", []) or [],
    )
    action_score = _exact_or_prefix_score(
        str(candidate_profile.get("action_type", GENERIC_ACTION)),
        str(material_profile.get("action_type", GENERIC_ACTION)),
        fallback=0.15 if candidate_type == material_type else 0.0,
        generic_score=0.5,
    )
    scene_score = _exact_or_prefix_score(
        str(candidate_profile.get("scene_hint", GENERIC_SCENE)),
        str(material_profile.get("scene_hint", GENERIC_SCENE)),
        fallback=0.2,
        generic_score=0.52,
    )
    context_score = max(0.0, min(1.0, (token_score * 0.65) + (anchor_score * 0.2) + (type_alignment * 0.15)))
    visual_quality = max(0.0, min(1.0, float(material_profile.get("visual_quality", 0.75))))
    generic_penalty = 0.08 if material_profile.get("is_generic") else 0.0
    motion_penalty = 0.04 if (
        candidate_profile.get("intent") == "product_effect"
        and str(material_profile.get("motion_level", "medium")) == "low"
    ) else 0.0
    final_score = (
        intent_score * 0.35
        + entity_score * 0.25
        + action_score * 0.15
        + scene_score * 0.10
        + context_score * 0.10
        + visual_quality * 0.05
        + type_alignment * 0.06
        + anchor_score * 0.04
        - generic_penalty
        - motion_penalty
    )
    final_score = max(0.0, min(1.0, round(final_score, 4)))
    return {
        "intent": round(intent_score, 4),
        "entity": round(entity_score, 4),
        "action": round(action_score, 4),
        "scene": round(scene_score, 4),
        "context": round(context_score, 4),
        "visual_quality": round(visual_quality, 4),
        "type_alignment": round(type_alignment, 4),
        "anchor": round(anchor_score, 4),
        "generic_penalty": round(generic_penalty, 4),
        "motion_penalty": round(motion_penalty, 4),
        "token_overlap": round(token_score, 4),
        "final_score": final_score,
    }


def compute_semantic_similarity(candidate_profile: Dict[str, object], material_profile: Dict[str, object]) -> float:
    return compute_semantic_match_details(candidate_profile, material_profile)["final_score"]


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
        semantic_profile = build_semantic_profile(
            text=combined_text,
            semantic_type=semantic_type,
            tags=video.get("tags", []) or [],
            emotion_strength=str(video.get("emotion_strength", "medium")),
            is_generic=generic,
            visual_quality=_build_quality_score(video, video.get("tags", []) or []),
        )
        enriched["semantic_type"] = semantic_type
        enriched["is_emergency_generic"] = generic
        enriched["semantic_profile"] = semantic_profile
        enriched["intent"] = semantic_profile.get("intent", "")
        enriched["action_type"] = semantic_profile.get("action_type", "")
        enriched["scene_hint"] = semantic_profile.get("scene_hint", "")
        enriched["entities"] = semantic_profile.get("entities", [])
        enriched["camera_shot"] = semantic_profile.get("camera_shot", "medium")
        enriched["motion_level"] = semantic_profile.get("motion_level", "medium")
        enriched["visual_quality"] = semantic_profile.get("visual_quality", 0.75)
        materials.append(enriched)
        if generic:
            generic_materials.append(enriched)

    emergency_limit = int(math.ceil(len(materials) * max(0.0, emergency_ratio))) if materials else 0
    if emergency_limit < 1 and emergency_ratio > 0:
        emergency_limit = 1
    emergency_pool = generic_materials[:emergency_limit]
    return {"materials": materials, "emergency_pool": emergency_pool}


def rank_materials_by_semantics(candidate: Dict[str, object], materials: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    candidate_profile = candidate.get("semantic_profile") or build_segment_semantic_script(
        text=str(candidate.get("text", "")),
        semantic_type=str(candidate.get("semantic_type", "")),
        tags=candidate.get("tags", []) or [],
        emotion_strength=str(candidate.get("emotion_strength", "medium")),
        context_prev=str(candidate.get("context_prev", "")),
        context_next=str(candidate.get("context_next", "")),
        trigger_reason=str(candidate.get("trigger_reason", "semantic")),
        intent=str(candidate.get("intent", "")),
        action_type=str(candidate.get("action_type", "")),
        scene_hint=str(candidate.get("scene_hint", "")),
        entities=candidate.get("entities", []) or [],
    )
    ranked: List[Dict[str, object]] = []
    for material in materials:
        material_profile = material.get("semantic_profile") or build_semantic_profile(
            text=str(material.get("filename", "")),
            semantic_type=str(material.get("semantic_type", "")),
            tags=material.get("tags", []) or [],
            emotion_strength=str(material.get("emotion_strength", "medium")),
            is_generic=bool(material.get("is_emergency_generic")),
            visual_quality=float(material.get("visual_quality", 0.75)),
        )
        details = compute_semantic_match_details(candidate_profile, material_profile)
        ranked.append(
            {
                "score": details["final_score"],
                "material": material,
                "score_breakdown": details,
                "candidate_profile": candidate_profile,
                "material_profile": material_profile,
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
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
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError, TypeError, ValueError):
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
            except (TypeError, ValueError):
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
