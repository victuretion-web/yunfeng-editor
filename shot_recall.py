import os
from typing import Dict, List, Optional, Sequence

try:
    from sentence_transformers import SentenceTransformer
except Exception:
    SentenceTransformer = None

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
except Exception:
    TfidfVectorizer = None
    cosine_similarity = None

from semantic_matcher import build_semantic_profile, infer_semantic_type, rank_materials_by_semantics
from shot_understanding import analyze_video_shots

_SENTENCE_MODEL = None
_SENTENCE_MODEL_ATTEMPTED = False


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _dedupe_texts(values: Sequence[object]) -> List[str]:
    results: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in results:
            results.append(text)
    return results


def _material_key(material: Dict[str, object]) -> str:
    key = str(material.get("unique_id") or material.get("content_hash") or material.get("path") or "")
    if key:
        return key
    return str(material.get("filename", ""))


def _resolve_semantic_type(material: Dict[str, object]) -> str:
    semantic_type = str(material.get("semantic_type", "")).strip()
    if semantic_type in ("product", "symptom", "generic"):
        return semantic_type
    source_type = str(material.get("_source_type", "")).strip()
    if source_type in ("product", "symptom", "generic"):
        return source_type
    combined = " ".join(
        [
            str(material.get("filename", "")),
            str(material.get("type", "")),
            " ".join(material.get("tags", []) or []),
        ]
    )
    return infer_semantic_type(combined, fallback="generic")


def _map_shot_scale(scale: str) -> str:
    if scale == "特写镜头":
        return "closeup"
    if scale == "近景镜头":
        return "closeup"
    if scale == "中景镜头":
        return "medium"
    return "wide"


def _map_motion_level(motion_score: float) -> str:
    if motion_score >= 0.14:
        return "high"
    if motion_score <= 0.05:
        return "low"
    return "medium"


def _map_purpose_to_semantics(semantic_type: str, shot_purpose: str) -> tuple[str, str]:
    if shot_purpose == "动作镜头":
        if semantic_type == "product":
            return "product_usage", "apply_ointment"
        return "symptom_feeling", "scratch_relief"
    if shot_purpose == "症状镜头":
        return "symptom_appearance", "skin_closeup"
    if shot_purpose == "讲解镜头":
        return "product_usage", "daily_life_scene"
    if shot_purpose == "功效镜头":
        return "product_effect", "apply_ointment"
    if shot_purpose == "展示镜头":
        return "product_form", "packshot_display"
    if shot_purpose == "场景镜头":
        return "generic_transition", "daily_life_scene"
    return "generic_transition", "generic_action"


def _estimate_shot_aes_score(material: Dict[str, object], shot: Dict[str, object]) -> float:
    score = 0.52
    duration = _safe_float(shot.get("duration_seconds"), _safe_float(material.get("duration"), 0.0))
    if 1.2 <= duration <= 4.5:
        score += 0.12
    elif duration > 0:
        score += 0.05

    shot_scale = str(shot.get("shot_scale", ""))
    if shot_scale in ("特写镜头", "近景镜头"):
        score += 0.08

    shot_purpose = str(shot.get("shot_purpose", ""))
    if shot_purpose in ("功效镜头", "展示镜头", "症状镜头", "动作镜头"):
        score += 0.09

    subject_type = str(shot.get("subject_type", ""))
    if subject_type in ("皮肤主体", "产品主体", "包装主体", "手部主体"):
        score += 0.08

    purpose_confidence = _safe_float(shot.get("shot_purpose_confidence"), 0.5)
    score += min(max(purpose_confidence, 0.0), 1.0) * 0.12

    contrast = _safe_float(shot.get("contrast"), 0.0)
    brightness = _safe_float(shot.get("brightness"), 0.5)
    motion_score = _safe_float(shot.get("motion_score"), 0.0)
    score += min(contrast, 0.28) * 0.25
    if 0.22 <= brightness <= 0.82:
        score += 0.05
    if 0.02 <= motion_score <= 0.18:
        score += 0.05

    if str(shot.get("description_source", "")) == "vision_llm":
        score += 0.03

    return round(max(0.35, min(0.98, score)), 4)


def _compose_shot_caption(material: Dict[str, object], shot: Dict[str, object]) -> str:
    description = str(shot.get("description", "")).strip()
    if description:
        return description
    parts = _dedupe_texts(
        [
            shot.get("subject_type"),
            shot.get("shot_scale"),
            shot.get("shot_purpose"),
            os.path.splitext(str(material.get("filename", "")))[0],
        ]
    )
    return "，".join(parts) if parts else os.path.splitext(str(material.get("filename", "")))[0]


def _compose_recall_text(material: Dict[str, object]) -> str:
    return " ".join(
        _dedupe_texts(
            [
                material.get("semantic_hint_text"),
                material.get("caption"),
                material.get("description"),
                " ".join(material.get("tags", []) or []),
                material.get("shot_purpose"),
                material.get("subject_type"),
                material.get("shot_scale"),
                material.get("original_filename"),
            ]
        )
    )


def _candidate_recall_query(candidate: Dict[str, object]) -> str:
    return " ".join(
        _dedupe_texts(
            [
                candidate.get("text"),
                " ".join(candidate.get("tags", []) or []),
                candidate.get("intent"),
                candidate.get("action_type"),
                candidate.get("scene_hint"),
                " ".join(candidate.get("entities", []) or []),
                candidate.get("visual_need"),
            ]
        )
    )


def _load_sentence_model() -> Optional[SentenceTransformer]:
    global _SENTENCE_MODEL
    global _SENTENCE_MODEL_ATTEMPTED
    if SentenceTransformer is None:
        return None
    if _SENTENCE_MODEL is not None:
        return _SENTENCE_MODEL
    if _SENTENCE_MODEL_ATTEMPTED:
        return None
    _SENTENCE_MODEL_ATTEMPTED = True
    model_names = [
        "paraphrase-multilingual-MiniLM-L12-v2",
        "all-MiniLM-L6-v2",
    ]
    for model_name in model_names:
        try:
            _SENTENCE_MODEL = SentenceTransformer(model_name, device="cpu", local_files_only=True)
            return _SENTENCE_MODEL
        except Exception:
            continue
    return None


def build_shot_material_library(
    materials: Sequence[Dict[str, object]],
    *,
    role: str,
    use_vlm: bool = False,
    api_key: str = "",
    vlm_model: str = "",
    base_url: str = "",
    force_refresh: bool = False,
    max_videos: Optional[int] = None,
) -> Dict[str, object]:
    shot_materials: List[Dict[str, object]] = []
    errors: List[str] = []
    selected_materials = list(materials)
    if max_videos is not None and max_videos > 0:
        selected_materials = selected_materials[:max_videos]

    for material in selected_materials:
        video_path = str(material.get("path", "")).strip()
        if not video_path or not os.path.exists(video_path):
            continue
        try:
            report = analyze_video_shots(
                video_path,
                role=role,
                use_vlm=use_vlm,
                api_key=api_key,
                vlm_model=vlm_model,
                base_url=base_url,
                force_refresh=force_refresh,
            )
            semantic_type = _resolve_semantic_type(material)
            base_hash = str(material.get("content_hash", "")).strip()
            base_uid = str(material.get("unique_id", "")).strip()
            for shot in report.get("shots", []) or []:
                start_seconds = _safe_float(shot.get("start_seconds"), 0.0)
                duration_seconds = max(0.2, _safe_float(shot.get("duration_seconds"), 0.0))
                shot_index = int(_safe_float(shot.get("shot_index"), 0))
                shot_scale = str(shot.get("shot_scale", ""))
                shot_purpose = str(shot.get("shot_purpose", ""))
                motion_score = _safe_float(shot.get("motion_score"), 0.0)
                intent, action_type = _map_purpose_to_semantics(semantic_type, shot_purpose)
                caption = _compose_shot_caption(material, shot)
                tags = _dedupe_texts(
                    list(material.get("tags", []) or [])
                    + list(shot.get("tags", []) or [])
                    + [
                        shot.get("subject_type"),
                        shot_scale,
                        shot_purpose,
                    ]
                )[:12]
                aes_score = _estimate_shot_aes_score(material, shot)
                semantic_text = " ".join(
                    _dedupe_texts(
                        [
                            caption,
                            " ".join(tags),
                            os.path.splitext(str(material.get("filename", "")))[0],
                            str(shot.get("role_context", "")),
                        ]
                    )
                )
                profile = build_semantic_profile(
                    text=semantic_text,
                    semantic_type=semantic_type,
                    tags=tags,
                    intent=intent,
                    action_type=action_type,
                    camera_shot=_map_shot_scale(shot_scale),
                    motion_level=_map_motion_level(motion_score),
                    visual_quality=aes_score,
                )
                shot_material = dict(material)
                shot_material.update(
                    {
                        "path": video_path,
                        "filename": f"{material.get('filename', os.path.basename(video_path))}#shot{shot_index:03d}",
                        "original_filename": material.get("filename", os.path.basename(video_path)),
                        "duration": round(duration_seconds, 3),
                        "source_start_us": int(round(start_seconds * 1_000_000)),
                        "duration_us": int(round(duration_seconds * 1_000_000)),
                        "tags": tags,
                        "semantic_type": semantic_type,
                        "semantic_hint_text": semantic_text,
                        "semantic_profile": profile,
                        "intent": profile.get("intent", intent),
                        "action_type": profile.get("action_type", action_type),
                        "scene_hint": profile.get("scene_hint", ""),
                        "entities": profile.get("entities", []),
                        "camera_shot": profile.get("camera_shot", "medium"),
                        "motion_level": profile.get("motion_level", "medium"),
                        "visual_quality": aes_score,
                        "aes_score": aes_score,
                        "caption": caption,
                        "description": caption,
                        "shot_index": shot_index,
                        "shot_start_seconds": round(start_seconds, 3),
                        "shot_end_seconds": round(_safe_float(shot.get("end_seconds"), start_seconds + duration_seconds), 3),
                        "shot_duration_seconds": round(duration_seconds, 3),
                        "thumbnail_path": shot.get("thumbnail_path", ""),
                        "shot_scale": shot_scale,
                        "subject_type": shot.get("subject_type", ""),
                        "shot_purpose": shot_purpose,
                        "shot_purpose_confidence": shot.get("shot_purpose_confidence", 0.0),
                        "shot_purpose_candidates": dict(shot.get("shot_purpose_candidates") or {}),
                        "evidence": list(shot.get("evidence", []) or []),
                        "description_source": shot.get("description_source", "heuristic"),
                        "is_shot_material": True,
                        "content_hash": f"{base_hash}:shot:{shot_index}:{int(round(start_seconds * 1000))}",
                        "unique_id": f"{base_uid}:shot:{shot_index}",
                    }
                )
                shot_materials.append(shot_material)
        except Exception as exc:
            errors.append(f"{os.path.basename(video_path)}: {exc}")

    return {
        "materials": shot_materials,
        "errors": errors,
    }


def build_recall_index(materials: Sequence[Dict[str, object]]) -> Dict[str, object]:
    prepared_materials = list(materials)
    texts = [_compose_recall_text(item) for item in prepared_materials]
    if not prepared_materials:
        return {"backend": "none", "materials": [], "texts": []}

    model = _load_sentence_model()
    if model is not None:
        try:
            matrix = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
            return {
                "backend": "sentence_transformers",
                "materials": prepared_materials,
                "texts": texts,
                "model": model,
                "matrix": matrix,
            }
        except Exception:
            pass

    if TfidfVectorizer is not None and cosine_similarity is not None:
        vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4))
        matrix = vectorizer.fit_transform(texts)
        return {
            "backend": "tfidf",
            "materials": prepared_materials,
            "texts": texts,
            "vectorizer": vectorizer,
            "matrix": matrix,
        }

    return {
        "backend": "none",
        "materials": prepared_materials,
        "texts": texts,
    }


def query_recall_index(index: Dict[str, object], query: str, n: int = 24) -> List[Dict[str, object]]:
    materials = list(index.get("materials", []) or [])
    if not materials:
        return []

    backend = str(index.get("backend", "none"))
    query = str(query or "").strip()
    if not query:
        return [{"material": item, "score": 0.0} for item in materials[: max(1, n)]]

    scores: List[float] = []
    if backend == "sentence_transformers":
        model = index.get("model")
        matrix = index.get("matrix")
        query_vector = model.encode([query], normalize_embeddings=True, show_progress_bar=False)[0]
        scores = [float(row.dot(query_vector)) for row in matrix]
    elif backend == "tfidf":
        vectorizer = index.get("vectorizer")
        matrix = index.get("matrix")
        query_vector = vectorizer.transform([query])
        scores = list(cosine_similarity(query_vector, matrix)[0])
    else:
        query_tokens = set(query.split())
        for text in index.get("texts", []):
            text_tokens = set(str(text or "").split())
            overlap = len(query_tokens & text_tokens)
            union = len(query_tokens | text_tokens) or 1
            scores.append(overlap / union)

    ranked_indices = sorted(range(len(materials)), key=lambda idx: scores[idx], reverse=True)[: max(1, n)]
    return [
        {
            "material": materials[idx],
            "score": round(max(0.0, min(1.0, float(scores[idx]))), 4),
        }
        for idx in ranked_indices
    ]


def search_shot_materials(
    candidate: Dict[str, object],
    recall_index: Optional[Dict[str, object]],
    *,
    top_n: int = 24,
) -> List[Dict[str, object]]:
    if not recall_index or not recall_index.get("materials"):
        return []

    recall_hits = query_recall_index(recall_index, _candidate_recall_query(candidate), n=top_n)
    shortlisted = [item["material"] for item in recall_hits]
    if not shortlisted:
        return []

    recall_score_map = {
        _material_key(item["material"]): float(item.get("score", 0.0))
        for item in recall_hits
    }
    ranked = rank_materials_by_semantics(candidate, shortlisted)
    for item in ranked:
        material = item.get("material") or {}
        recall_score = recall_score_map.get(_material_key(material), 0.0)
        semantic_score = float(item.get("score", 0.0))
        aes_score = _safe_float(material.get("aes_score"), _safe_float(material.get("visual_quality"), 0.75))
        reranked_score = max(
            0.0,
            min(
                1.0,
                round(semantic_score * 0.80 + recall_score * 0.12 + aes_score * 0.08, 4),
            ),
        )
        item["score"] = reranked_score
        breakdown = dict(item.get("score_breakdown") or {})
        breakdown["semantic_only"] = round(semantic_score, 4)
        breakdown["shot_recall"] = round(recall_score, 4)
        breakdown["aes_score"] = round(aes_score, 4)
        breakdown["final_score"] = reranked_score
        item["score_breakdown"] = breakdown
    ranked.sort(key=lambda current: float(current.get("score", 0.0)), reverse=True)
    return ranked
