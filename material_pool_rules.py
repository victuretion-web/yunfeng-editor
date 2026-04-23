import json
import os
import re
from difflib import SequenceMatcher
from typing import Dict, List, Tuple


SIMILARITY_THRESHOLD = 0.9
POOL_REQUIREMENTS = {
    "medium": {"total_min": 8, "symptom_min": 4, "product_min": 4},
    "high": {"total_min": 10, "symptom_min": 5, "product_min": 5},
}


def normalize_material_name(filename: str) -> str:
    name = os.path.splitext(os.path.basename(str(filename)))[0].lower()
    name = re.sub(r"[\s_\-()（）【】\[\]]+", "", name)
    return name


def material_similarity_score(left: Dict, right: Dict) -> float:
    left_name = normalize_material_name(left.get("filename") or left.get("path", ""))
    right_name = normalize_material_name(right.get("filename") or right.get("path", ""))
    if not left_name or not right_name:
        return 0.0
    return SequenceMatcher(None, left_name, right_name).ratio()


def are_materials_similar(left: Dict, right: Dict, threshold: float = SIMILARITY_THRESHOLD) -> bool:
    left_filename = os.path.basename(str(left.get("filename") or left.get("path", "")))
    right_filename = os.path.basename(str(right.get("filename") or right.get("path", "")))
    if left_filename and right_filename and left_filename != right_filename:
        return False

    left_uid = left.get("unique_id")
    right_uid = right.get("unique_id")
    if left_uid and right_uid and left_uid == right_uid:
        return True

    left_hash = left.get("content_hash")
    right_hash = right.get("content_hash")
    if left_hash and right_hash and left_hash == right_hash:
        return True

    left_path = os.path.normcase(os.path.abspath(left.get("path", "")))
    right_path = os.path.normcase(os.path.abspath(right.get("path", "")))
    if left_path and left_path == right_path:
        return True

    duration_gap = abs(float(left.get("duration", 0.0)) - float(right.get("duration", 0.0)))
    file_size_gap = abs(int(left.get("file_size", 0)) - int(right.get("file_size", 0)))
    similarity = material_similarity_score(left, right)
    return similarity >= threshold and duration_gap <= 1.0 and file_size_gap <= 4096


def dedupe_materials(materials: List[Dict], threshold: float = SIMILARITY_THRESHOLD) -> Tuple[List[Dict], List[Dict]]:
    unique_materials: List[Dict] = []
    removed_materials: List[Dict] = []

    for material in materials:
        if any(are_materials_similar(material, kept, threshold=threshold) for kept in unique_materials):
            removed_materials.append(material)
        else:
            unique_materials.append(material)

    return unique_materials, removed_materials


def validate_material_pools(
    product_videos: List[Dict],
    symptom_videos: List[Dict],
    sensitivity: str,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
) -> Tuple[List[Dict], List[Dict], Dict]:
    normalized_sensitivity = str(sensitivity or "medium").lower()
    if normalized_sensitivity not in POOL_REQUIREMENTS:
        raise ValueError(f"不支持的素材密度选项: {sensitivity}")

    deduped_products, removed_products = dedupe_materials(product_videos, threshold=similarity_threshold)
    deduped_symptoms, removed_symptoms = dedupe_materials(symptom_videos, threshold=similarity_threshold)

    requirements = POOL_REQUIREMENTS[normalized_sensitivity]
    report = {
        "sensitivity": normalized_sensitivity,
        "requirements": requirements,
        "similarity_threshold": similarity_threshold,
        "product_before": len(product_videos),
        "symptom_before": len(symptom_videos),
        "product_after": len(deduped_products),
        "symptom_after": len(deduped_symptoms),
        "total_after": len(deduped_products) + len(deduped_symptoms),
        "removed_products": [item.get("filename", item.get("path", "")) for item in removed_products],
        "removed_symptoms": [item.get("filename", item.get("path", "")) for item in removed_symptoms],
        "product_hashes": [item.get("content_hash", "") for item in deduped_products],
        "symptom_hashes": [item.get("content_hash", "") for item in deduped_symptoms],
    }

    if report["product_after"] < requirements["product_min"]:
        raise ValueError(
            f"{normalized_sensitivity} 素材池不达标: 产品展示素材至少需要 {requirements['product_min']} 个独立素材，"
            f"当前去重后仅有 {report['product_after']} 个。"
        )
    if report["symptom_after"] < requirements["symptom_min"]:
        raise ValueError(
            f"{normalized_sensitivity} 素材池不达标: 病症素材至少需要 {requirements['symptom_min']} 个独立素材，"
            f"当前去重后仅有 {report['symptom_after']} 个。"
        )
    if report["total_after"] < requirements["total_min"]:
        raise ValueError(
            f"{normalized_sensitivity} 素材池不达标: 总独立素材至少需要 {requirements['total_min']} 个，"
            f"当前去重后仅有 {report['total_after']} 个。"
        )

    return deduped_products, deduped_symptoms, report


def write_material_pool_report(report: Dict, report_path: str) -> None:
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
