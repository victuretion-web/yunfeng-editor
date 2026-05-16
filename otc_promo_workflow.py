"""
OTC药品推广视频智能剪辑工作流
功能：
1. AI语音识别：对口播内容进行逐句转写与语义分析
2. 智能素材匹配：根据口播内容自动插入病症/产品素材
3. 增强元素：动态贴图、环境音效、背景音乐
4. 输出：MP4格式，3-5分钟，符合OTC药品推广规范
"""

import os
import sys
import json
import re
import glob
import csv
import hashlib
import math
import random
import shutil
import subprocess
import threading
from typing import List, Dict, Optional, Tuple, Union
from datetime import datetime
from app_paths import (
    build_runtime_env,
    configure_current_process,
    ensure_skill_scripts_on_path,
    get_output_dir,
    resource_path,
    runtime_path,
)
from subprocess_windows import run_hidden


try:
    import winreg
except ImportError:
    winreg = None

# 仅解析运行时环境，不在模块导入时改写全局 os.environ。
RUNTIME_ENV = build_runtime_env(os.environ.copy())
ensure_skill_scripts_on_path()
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from jy_wrapper import JyProject, draft
from draft_registry import (
    file_lock,
    get_draft_root,
    get_official_draft_root,
    is_portable_draft_root,
    reconcile_root_meta,
    sync_managed_drafts,
)
from material_pool_rules import validate_material_pools, write_material_pool_report
from media_identity import build_media_identity
from media_file_rules import (
    is_supported_video_file,
    scan_video_file_paths,
    validate_speech_video_file,
)
from insert_density import (
    analyze_insert_density,
    auto_fill_density_gaps,
    export_density_reports,
    get_density_template_config,
)
from semantic_matcher import (
    BrollUsageHistory,
    PRIMARY_SEMANTIC_THRESHOLD,
    CASCADE_REUSE_THRESHOLD,
    WINDOW_TARGET_RATIO,
    analyze_broll_ratio,
    build_segment_semantic_script,
    build_material_semantic_library,
    rank_materials_by_semantics,
)
from shot_recall import build_recall_index, build_shot_material_library, search_shot_materials
from bgm_pipeline import prepare_bgm_for_timeline
from timeline_utils import (
    layout_segments_on_tracks,
    sanitize_non_overlapping_segments,
    seconds_to_microseconds,
)

# 配置参数
_CONFIG_PARSE_WARNINGS: List[str] = []


def _env_text(name: str, default: str = "") -> str:
    return str(RUNTIME_ENV.get(name, default) or "").strip()


def _join_if_base(base_dir: str, child: str) -> str:
    return os.path.join(base_dir, child) if base_dir else ""


def _env_int(name: str, default: int) -> int:
    raw = _env_text(name, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        _CONFIG_PARSE_WARNINGS.append(f"{name}={raw!r} 不是合法整数，已回退为 {default}")
        return int(default)


def _env_float(name: str, default: float) -> float:
    raw = _env_text(name, str(default))
    try:
        return float(raw)
    except (TypeError, ValueError):
        _CONFIG_PARSE_WARNINGS.append(f"{name}={raw!r} 不是合法数字，已回退为 {default}")
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_text(name, "1" if default else "0").lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    _CONFIG_PARSE_WARNINGS.append(f"{name}={raw!r} 不是合法布尔值，已回退为 {default}")
    return bool(default)


VIDEO_DIR = _env_text("OTC_VIDEO_DIR", "")
def _resolve_media_dir(env_name: str, base_dir: str, *relative_candidates: str) -> str:
    env_value = _env_text(env_name, "")
    if env_value:
        return env_value
    for relative in relative_candidates:
        candidate = os.path.join(base_dir, relative)
        if os.path.isdir(candidate):
            return candidate
    if base_dir and relative_candidates:
        return os.path.join(base_dir, relative_candidates[0])
    return base_dir


SPEECH_DIR = _env_text("OTC_SPEECH_DIR", _join_if_base(VIDEO_DIR, "口播"))
PRODUCT_DIR = _resolve_media_dir("OTC_PRODUCT_DIR", VIDEO_DIR, "产品")
SYMPTOM_DIR = _resolve_media_dir("OTC_SYMPTOM_DIR", VIDEO_DIR, "病症")
AUDIO_DIR = _resolve_media_dir("OTC_AUDIO_DIR", VIDEO_DIR, "音效")
BGM_DIR = _resolve_media_dir("OTC_BGM_DIR", VIDEO_DIR, "背景音乐")
AD_REVIEW_DIR = _resolve_media_dir("OTC_AD_REVIEW_DIR", VIDEO_DIR, "广审素材", "广审")
STICKER_DIR = _resolve_media_dir("OTC_STICKER_DIR", VIDEO_DIR, "顶部贴图", "贴图")
OUTPUT_DIR = _env_text("OTC_OUTPUT_DIR", get_output_dir())
DRAFT_HEALTH_REPORT_PATH = os.path.join(OUTPUT_DIR, "draft_registry_health.json")

# 频率限制参数 (从 UI 获取，默认无限制为0)
AD_FREQ_LIMIT = _env_int("OTC_AD_FREQ", 1)
STICKER_FREQ_LIMIT = _env_int("OTC_STICKER_FREQ", 0)
BROLL_FREQ_LIMIT = _env_int("OTC_BROLL_FREQ", 1) # 默认去重，同一中插只播放1次

# 成品配置参数
TEMPLATE_MODE = _env_text("OTC_TEMPLATE_MODE", "标准口播版")
RHYTHM_MODE = _env_text("OTC_RHYTHM_MODE", "常规呼吸感")
MIN_HOST_DURATION = _env_float("OTC_MIN_HOST_DURATION", 1.2)
DENSITY_TEMPLATE = _env_text("OTC_DENSITY_TEMPLATE", "中节奏")
DENSITY_WINDOW_SECONDS = _env_float("OTC_DENSITY_WINDOW_SECONDS", 30.0)
MIN_INSERTS_PER_WINDOW = _env_int("OTC_MIN_INSERTS_PER_WINDOW", 1)
INSERT_MIN_DURATION = _env_float("OTC_INSERT_MIN_DURATION", 3.0)
INSERT_MAX_DURATION = _env_float("OTC_INSERT_MAX_DURATION", 8.0)
AUTO_FILL_DENSITY = _env_bool("OTC_AUTO_FILL_DENSITY", True)
BGM_CROSSFADE_MS = _env_int("OTC_BGM_CROSSFADE_MS", 200)
BGM_TARGET_LUFS = _env_int("OTC_BGM_TARGET_LUFS", -18)
BGM_NORMALIZE = _env_bool("OTC_BGM_NORMALIZE", True)
BGM_PHASE_CHECK = _env_bool("OTC_BGM_PHASE_CHECK", True)
BROLL_RATIO = _env_text("OTC_BROLL_RATIO", "1:1") or "1:1"
USER_BGM_DIR = _resolve_media_dir("OTC_USER_BGM_DIR", VIDEO_DIR, "背景音乐", runtime_path("my_bg_music"))
BGM_PICK_MODE = _env_text("OTC_BGM_PICK_MODE", "按文件名顺序") or "按文件名顺序"
POOL_MIN_COUNT = _env_int("OTC_POOL_MIN_COUNT", 20)
POOL_MIN_DURATION = _env_float("OTC_POOL_MIN_DURATION", 3.0)
POOL_MAX_DURATION = _env_float("OTC_POOL_MAX_DURATION", 8.0)
VOICE_TARGET_LUFS = _env_int("OTC_VOICE_TARGET_LUFS", -9)
DECISION_LOG_DIR = os.path.join(OUTPUT_DIR, "decision_logs")
INTERNAL_MAINTENANCE_LOG_PATH = os.path.join(OUTPUT_DIR, "internal_maintenance.log")

MATERIAL_POOL_REPORT_PATH = os.path.join(OUTPUT_DIR, "material_pool_validation.json")
BROLL_USAGE_HISTORY_PATH = os.path.join(OUTPUT_DIR, "broll_usage_history.json")
SEMANTIC_MATCH_THRESHOLD = max(
    PRIMARY_SEMANTIC_THRESHOLD,
    _env_float("OTC_SEMANTIC_MATCH_THRESHOLD", PRIMARY_SEMANTIC_THRESHOLD),
)
SEMANTIC_REUSE_THRESHOLD = max(
    CASCADE_REUSE_THRESHOLD,
    _env_float("OTC_SEMANTIC_REUSE_THRESHOLD", CASCADE_REUSE_THRESHOLD),
)
TARGET_BROLL_COVERAGE_RATIO = max(0.0, min(0.95, _env_float("OTC_TARGET_BROLL_RATIO", WINDOW_TARGET_RATIO)))
ALLOW_PREVIOUS_VIDEO_REUSE_WITHIN_24H = _env_bool("OTC_ALLOW_PREVIOUS_VIDEO_REUSE_WITHIN_24H", False)
ENABLE_SHOT_RECALL = _env_bool("OTC_ENABLE_SHOT_RECALL", True)
SHOT_RECALL_TOP_N = max(6, _env_int("OTC_SHOT_RECALL_TOP_N", 24))
SHOT_RECALL_MAX_VIDEOS = max(0, _env_int("OTC_SHOT_RECALL_MAX_VIDEOS", 0))
SHOT_RECALL_USE_VLM = _env_bool("OTC_SHOT_RECALL_USE_VLM", False)
SHOT_RECALL_FORCE_REFRESH = _env_bool("OTC_SHOT_RECALL_FORCE_REFRESH", False)
SHOT_RECALL_VLM_MODEL = _env_text("OTC_SHOT_RECALL_VLM_MODEL", "")
STRICT_SEMANTIC_INSERTION = _env_bool("OTC_STRICT_SEMANTIC_INSERTION", True)
ALLOW_BLIND_FALLBACK = _env_bool("OTC_ALLOW_BLIND_FALLBACK", False)
GENERIC_EMERGENCY_THRESHOLD = max(
    0.0,
    min(0.95, _env_float("OTC_GENERIC_EMERGENCY_THRESHOLD", 0.58)),
)
_SHOT_RECALL_BUNDLE_CACHE: Dict[Tuple, Dict[str, object]] = {}
_SHOT_RECALL_BUNDLE_CACHE_LOCK = threading.Lock()

class UsageTracker:
    """任务级素材使用追踪器，用于控制素材调用频率"""
    def __init__(self, limits: Dict[str, int]):
        self.limits = limits
        self.usage = {}
        self.history = [] # 记录使用历史，支持断点续播场景
        self._lock = threading.Lock()

    def can_use(self, item: Union[str, Dict], category: str) -> bool:
        limit = self.limits.get(category, 0)
        if limit == 0:
            return True
        uid = item.get('unique_id', item['path']) if isinstance(item, dict) else item
        with self._lock:
            return self.usage.get(uid, 0) < limit

    def get_count(self, item: Union[str, Dict]) -> int:
        uid = item.get('unique_id', item['path']) if isinstance(item, dict) else item
        with self._lock:
            return self.usage.get(uid, 0)

    def record(self, item: Union[str, Dict]):
        uid = item.get('unique_id', item['path']) if isinstance(item, dict) else item
        with self._lock:
            self.usage[uid] = self.usage.get(uid, 0) + 1
            self.history.append(uid)
        
    def filter_available(self, filepaths: List[str], category: str) -> List[str]:
        """过滤出当前仍可用的素材列表"""
        return [f for f in filepaths if self.can_use(f, category)]
        
    def filter_available_dicts(self, items: List[Dict], category: str) -> List[Dict]:
        return [i for i in items if self.can_use(i, category)]

# 语义关键词配置
SYMPTOM_KEYWORDS = [
    '症状', '表现', '困扰', '瘙痒', '疼痛', '不适', '红斑', '脱屑', 
    '皮肤', '感染', '真菌', '体癣', '股癣', '手足癣', '难受', '影响',
    '生活质量', '睡眠', '工作', '社交', '尴尬', '反复'
]

PRODUCT_KEYWORDS = [
    '产品', '治疗', '使用', '方法', '效果', '改善', '推荐', '购买',
    '我们的', '这款', '这个', '成分', '功效', '特点', '优势', '安全',
    '无刺激', '温和', '快速', '有效', '专业', '认证', '批准',
    '胶囊', '乳膏', '喷雾', '软膏', '药膏', '抑菌', '止痒', '涂抹',
    '疗程', '外用', '口服', '达克宁', '百癣夏塔热'
]

# 情感基调关键词
EMOTIONAL_KEYWORDS = {
    'positive': ['有效', '改善', '治愈', '成功', '满意', '推荐', '信任'],
    'negative': ['困扰', '难受', '痛苦', '尴尬', '影响', '反复'],
    'neutral': ['介绍', '说明', '展示', '演示', '使用']
}

EMOTION_STRENGTH_KEYWORDS = {
    "high": ["严重", "剧烈", "反复", "难忍", "爆发", "加重", "疼痛", "瘙痒", "红肿", "脱屑"],
    "medium": ["困扰", "不适", "发作", "刺激", "泛红", "刺痛"],
    "low": ["舒缓", "展示", "成分", "温和", "日常", "说明"],
}


def _parse_ratio_config(ratio_text: str) -> Tuple[int, int]:
    try:
        left, right = str(ratio_text or "1:1").split(":", 1)
        symptom_ratio = max(1, int(left))
        product_ratio = max(1, int(right))
        return symptom_ratio, product_ratio
    except Exception as exc:
        _append_runtime_issue(
            "parse_ratio_config",
            "中插比例配置解析失败，已回退为 1:1。",
            exc=exc,
            payload={"ratio_text": ratio_text},
        )
        return 1, 1


def _decision_log_path(video_id: str) -> str:
    os.makedirs(DECISION_LOG_DIR, exist_ok=True)
    safe_name = re.sub(r'[<>:"/\\|?*]+', "_", video_id)
    return os.path.join(DECISION_LOG_DIR, f"{safe_name}_decision_log.jsonl")


def _append_decision_log(log_path: str, payload: Dict):
    record = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        **payload,
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _append_runtime_issue(stage: str, message: str, *, exc: Optional[BaseException] = None, payload: Optional[Dict[str, object]] = None) -> None:
    try:
        os.makedirs(os.path.dirname(INTERNAL_MAINTENANCE_LOG_PATH), exist_ok=True)
        record = {
            "logged_at": datetime.now().isoformat(timespec="seconds"),
            "stage": stage,
            "message": message,
        }
        if exc is not None:
            record["exception"] = f"{type(exc).__name__}: {exc}"
        if payload:
            record["payload"] = payload
        with open(INTERNAL_MAINTENANCE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return


def _report_runtime_message(
    stage: str,
    message: str,
    *,
    level: str = "INFO",
    payload: Optional[Dict[str, object]] = None,
    exc: Optional[BaseException] = None,
) -> None:
    prefix = f"[{level}]"
    print(f"   {prefix} {message}")
    if level in {"WARN", "ERROR", "SKIP"}:
        _append_runtime_issue(stage, message, exc=exc, payload=payload)


def _probe_media_dimensions(filepath: str) -> Tuple[int, int]:
    try:
        result = run_hidden(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=s=x:p=0",
                filepath,
            ],
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
            errors="ignore",
        )
        raw = result.stdout.strip()
        if "x" in raw:
            width_text, height_text = raw.split("x", 1)
            return int(width_text), int(height_text)
    except Exception as exc:
        _append_runtime_issue(
            "probe_media_dimensions",
            "读取媒体分辨率失败，已回退为 0x0。",
            exc=exc,
            payload={"filepath": filepath},
        )
    return 0, 0


def _infer_material_emotion(tags: List[str], filename: str) -> str:
    tag_text = " ".join(tags + [filename])
    for level, keywords in EMOTION_STRENGTH_KEYWORDS.items():
        if any(keyword in tag_text for keyword in keywords):
            return level
    return "medium"


def _infer_material_tags_from_filepath(filepath: str, source_type: str = "") -> List[str]:
    filename = os.path.basename(filepath)
    filename_no_ext = os.path.splitext(filename)[0]
    parent_name = os.path.basename(os.path.dirname(filepath))
    combined = " ".join([filename_no_ext, parent_name, source_type]).strip()

    inferred_tags: List[str] = []

    # 目录级默认语义，解决真实素材文件名是纯数字时完全没有语义信号的问题。
    source_alias = str(source_type or parent_name).strip().lower()
    if source_alias in ("产品", "product"):
        inferred_tags.extend(["产品", "展示", "使用"])
    elif source_alias in ("病症", "symptom"):
        inferred_tags.extend(["症状", "困扰", "皮肤"])

    for keyword in PRODUCT_KEYWORDS + SYMPTOM_KEYWORDS:
        if keyword in combined:
            inferred_tags.append(keyword)

    heuristic_groups = [
        ["特写", "局部", "近景", "细节"],
        ["中景", "半身", "演示"],
        ["全景", "场景", "环境"],
        ["抓挠", "抓", "挠", "瘙痒"],
        ["涂抹", "药膏", "乳膏", "软膏", "外用"],
        ["喷雾", "喷剂", "喷上"],
        ["红斑", "泛红", "红肿", "脱屑", "起皮"],
        ["晚上", "夜里", "夜间", "睡觉", "床上"],
        ["工作", "社交", "出门", "走路"],
        ["包装", "外盒", "瓶身", "质地"],
    ]
    for group in heuristic_groups:
        for keyword in group:
            if keyword in combined:
                inferred_tags.append(keyword)

    if re.fullmatch(r"\d+", filename_no_ext):
        if source_alias in ("产品", "product"):
            inferred_tags.extend(["产品", "展示", "包装"])
        elif source_alias in ("病症", "symptom"):
            inferred_tags.extend(["症状", "困扰", "局部"])

    return list(dict.fromkeys(tag for tag in inferred_tags if str(tag).strip()))


def _has_semantic_material_signals(item: Dict) -> bool:
    return bool(
        item.get("tags")
        or item.get("manual_tags")
        or item.get("auto_tags")
        or item.get("semantic_hint_text")
    )


def _validate_tagged_material_pool(videos: List[Dict], pool_name: str, semantic_type: str):
    qualified = [
        item for item in videos
        if float(item.get("duration", 0.0)) >= POOL_MIN_DURATION
        and float(item.get("duration", 0.0)) <= POOL_MAX_DURATION
        and bool(item.get("is_vertical"))
        and _has_semantic_material_signals(item)
    ]
    if len(qualified) < POOL_MIN_COUNT:
        _report_runtime_message(
            "material_pool_validation",
            f"{pool_name}当前满足标准的素材不足 {POOL_MIN_COUNT} 条，仅检测到 {len(qualified)} 条，系统将继续生成但建议尽快补足。",
            level="WARN",
            payload={"pool_name": pool_name, "qualified_count": len(qualified), "required_count": POOL_MIN_COUNT},
        )

    report = {
        "pool_name": pool_name,
        "semantic_type": semantic_type,
        "required_count": POOL_MIN_COUNT,
        "qualified_count": len(qualified),
        "qualified_examples": [os.path.basename(item["path"]) for item in qualified[:10]],
        "auto_tagged_count": sum(1 for item in qualified if item.get("auto_tags")),
    }
    return report


def detect_jianying_version() -> Optional[str]:
    """综合注册表与常见路径，优先返回可兼容的 5.9 版本。"""
    discovered_versions = []

    path_versions = _detect_jianying_versions_from_paths()
    if path_versions:
        discovered_versions.extend(path_versions)

    if winreg is None:
        return _pick_preferred_jianying_version(discovered_versions)

    uninstall_roots = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]

    for hive, root in uninstall_roots:
        try:
            with winreg.OpenKey(hive, root) as key:
                subkey_count = winreg.QueryInfoKey(key)[0]
                for i in range(subkey_count):
                    subkey_name = winreg.EnumKey(key, i)
                    try:
                        with winreg.OpenKey(key, subkey_name) as subkey:
                            display_name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                            if "剪映" not in str(display_name) and "Jianying" not in str(display_name):
                                continue
                            version, _ = winreg.QueryValueEx(subkey, "DisplayVersion")
                            discovered_versions.append(str(version).strip())
                    except OSError:
                        continue
        except OSError:
            continue

    return _pick_preferred_jianying_version(discovered_versions)


def _detect_jianying_version_from_paths() -> Optional[str]:
    versions = _detect_jianying_versions_from_paths()
    return _pick_preferred_jianying_version(versions)


def _detect_jianying_versions_from_paths() -> List[str]:
    """兼容绿色版/解压版：从常见路径中的 JianyingPro.exe 所在目录推断版本。"""
    candidate_roots = [
        os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
        os.path.join(os.environ.get("USERPROFILE", ""), "Downloads"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "JianyingPro"),
        r"C:\Program Files\JianyingPro",
        r"C:\Program Files (x86)\JianyingPro",
    ]

    semver_pattern = re.compile(r"(\d+\.\d+\.\d+\.\d+|\d+\.\d+(?:\.\d+)?)")
    found_versions = []
    seen_versions = set()

    for root in candidate_roots:
        if not root or not os.path.exists(root):
            continue

        try:
            for current_root, dirs, files in os.walk(root):
                depth = os.path.relpath(current_root, root).count(os.sep)
                if depth > 6:
                    dirs[:] = []
                    continue

                if "JianyingPro.exe" not in files:
                    continue

                probe_path = current_root
                while True:
                    basename = os.path.basename(probe_path)
                    match = semver_pattern.search(basename)
                    if match:
                        version = match.group(1)
                        if version not in seen_versions:
                            seen_versions.add(version)
                            found_versions.append(version)
                        break
                    parent = os.path.dirname(probe_path)
                    if not parent or parent == probe_path:
                        break
                    probe_path = parent
        except OSError:
            continue

    return found_versions


def _pick_preferred_jianying_version(versions: List[str]) -> Optional[str]:
    normalized = []
    seen = set()
    for version in versions:
        version = str(version).strip()
        if not version or version in seen:
            continue
        seen.add(version)
        normalized.append(version)

    if not normalized:
        return None

    supported_versions = [version for version in normalized if is_supported_jianying_version(version)]
    if supported_versions:
        return sorted(supported_versions, key=_version_sort_key, reverse=True)[0]

    return sorted(normalized, key=_version_sort_key, reverse=True)[0]


def _version_sort_key(version: str):
    parts = [int(part) for part in re.findall(r"\d+", version)]
    return tuple(parts + [0] * (4 - len(parts)))


def is_supported_jianying_version(version: Optional[str]) -> bool:
    """当前工作流仅对 5.9 系列做硬兼容兜底。"""
    if not version:
        return False
    match = re.match(r"^\s*(\d+)(?:\.(\d+)(?:\.\d+)*)?\s*$", version.strip())
    if not match:
        return False
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return major < 5 or (major == 5 and minor <= 9)


def build_runtime_preflight_report() -> Dict[str, object]:
    report: Dict[str, object] = {
        "fatal_errors": [],
        "warnings": [],
        "checks": {},
    }

    draft_root = get_draft_root()
    official_root = get_official_draft_root()
    detected_version = detect_jianying_version()
    ffmpeg_path = shutil.which("ffmpeg") or resource_path("ffmpeg-8.1-essentials_build", "bin", "ffmpeg.exe")
    ffprobe_path = shutil.which("ffprobe") or resource_path("ffmpeg-8.1-essentials_build", "bin", "ffprobe.exe")
    skill_wrapper_path = resource_path(
        "jianying-editor-skill-main",
        "jianying-editor-skill-main",
        "scripts",
        "jy_wrapper.py",
    )

    report["checks"] = {
        "draft_root": draft_root,
        "official_draft_root": official_root,
        "using_portable_draft_root": is_portable_draft_root(draft_root),
        "localappdata": os.environ.get("LOCALAPPDATA", ""),
        "skill_wrapper_exists": os.path.exists(skill_wrapper_path),
        "ffmpeg_path": ffmpeg_path,
        "ffmpeg_exists": os.path.exists(ffmpeg_path) if ffmpeg_path else False,
        "ffprobe_path": ffprobe_path,
        "ffprobe_exists": os.path.exists(ffprobe_path) if ffprobe_path else False,
        "detected_jianying_version": detected_version,
        "supported_jianying_version": is_supported_jianying_version(detected_version),
    }

    try:
        os.makedirs(draft_root, exist_ok=True)
        probe_path = os.path.join(draft_root, ".draft_write_probe.tmp")
        with open(probe_path, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe_path)
        report["checks"]["draft_root_writable"] = True
    except Exception as exc:
        report["checks"]["draft_root_writable"] = False
        report["fatal_errors"].append(f"草稿目录不可写: {draft_root} ({exc})")

    if _CONFIG_PARSE_WARNINGS:
        report["warnings"].extend(_CONFIG_PARSE_WARNINGS)

    if not report["checks"]["skill_wrapper_exists"]:
        report["fatal_errors"].append(f"缺少打包资源: {skill_wrapper_path}")

    if not report["checks"]["ffmpeg_exists"]:
        report["fatal_errors"].append("未找到 ffmpeg，可执行文件未正确打包或被拦截。")

    if not report["checks"]["ffprobe_exists"]:
        report["fatal_errors"].append("未找到 ffprobe，可执行文件未正确打包或被拦截。")

    if report["checks"]["using_portable_draft_root"]:
        report["warnings"].append(
            f"当前未使用系统剪映草稿目录，已回退到便携草稿目录: {draft_root}"
        )

    if not detected_version:
        report["warnings"].append("未检测到剪映版本，将继续生成草稿，但无法保证可直接在剪映中显示。")
    elif not report["checks"]["supported_jianying_version"]:
        report["warnings"].append(
            f"检测到剪映版本 {detected_version}，非 5.9 兼容版本，草稿可能无法在剪映中直接打开。"
        )

    if ENABLE_SHOT_RECALL and SHOT_RECALL_USE_VLM:
        llm_api_key = os.environ.get("LLM_API_KEY", "").strip()
        llm_model = os.environ.get("LLM_MODEL", "").strip()
        vlm_model = SHOT_RECALL_VLM_MODEL.strip() or llm_model
        missing = []
        if not llm_api_key:
            missing.append("LLM_API_KEY")
        if not llm_model:
            missing.append("LLM_MODEL")
        if not vlm_model:
            missing.append("OTC_SHOT_RECALL_VLM_MODEL")
        if missing:
            report["warnings"].append(
                "主流程已启用 VLM 素材理解，但以下配置缺失，将回退到启发式镜头理解: "
                + ", ".join(missing)
            )
    if STRICT_SEMANTIC_INSERTION and not ALLOW_BLIND_FALLBACK:
        report["warnings"].append(
            "当前启用严格语义插入，未达到可信阈值的片段会被放弃，不再为凑占比进行盲选兜底。"
        )

    return report


def validate_saved_draft(project: JyProject) -> str:
    """强校验草稿是否真正落盘，避免仅生成报告却误判成功。"""
    draft_path = os.path.join(project.root, project.name)
    content_path = os.path.join(draft_path, "draft_content.json")
    meta_path = os.path.join(draft_path, "draft_meta_info.json")

    missing = [p for p in (content_path, meta_path) if not os.path.exists(p)]
    if missing:
        raise RuntimeError(
            "草稿目录已创建，但核心文件未落盘: "
            + ", ".join(os.path.basename(p) for p in missing)
        )

    return draft_path


def _find_requested_video(requested_video: str, speech_videos: List[Dict]) -> Optional[Dict]:
    requested = str(requested_video).strip()
    if not requested:
        return None

    normalized_requested = os.path.normcase(os.path.abspath(requested))
    for video in speech_videos:
        if os.path.normcase(os.path.abspath(video["path"])) == normalized_requested:
            return video

    requested_basename = os.path.basename(requested)
    for video in speech_videos:
        if video["filename"] == requested_basename:
            return video

    return None


def collect_video_files(directory: str, log_skipped_audio: bool = False, source_label: str = "素材") -> List[Dict]:
    """递归收集目录中的视频文件信息，并可记录被跳过的音频文件。"""
    videos = []
    video_paths, skipped_audio_paths = scan_video_file_paths(directory, recursive=True, skip_generated_artifacts=True)

    if log_skipped_audio:
        for skipped_path in skipped_audio_paths:
            _report_runtime_message(
                "scan_video_files",
                f"跳过{source_label}音频文件: {os.path.basename(skipped_path)}",
                level="SKIP",
                payload={"source_label": source_label, "filepath": skipped_path},
            )

    if not video_paths:
        return videos

    for filepath in video_paths:
        filename = os.path.basename(filepath)
        try:
            duration = _probe_media_duration(filepath)
            identity = build_media_identity(filepath, duration)

            # 解析标签：如 "[局部特写]_皮炎平.mp4"
            manual_tags = []
            tag_match = re.findall(r'\[(.*?)\]', filename)
            if tag_match:
                for tm in tag_match:
                    manual_tags.extend([t.strip() for t in tm.split(',') if t.strip()])

            source_type = os.path.basename(os.path.dirname(filepath))
            auto_tags = _infer_material_tags_from_filepath(filepath, source_type=source_type)
            tags = list(dict.fromkeys(list(manual_tags) + list(auto_tags)))

            width, height = _probe_media_dimensions(filepath)

            videos.append({
                'path': filepath,
                'filename': filename,
                'duration': duration,
                'type': source_type,
                'unique_id': identity['unique_id'],
                'content_hash': identity['content_hash'],
                'file_size': identity['file_size'],
                'tags': tags,
                'manual_tags': manual_tags,
                'auto_tags': [tag for tag in auto_tags if tag not in manual_tags],
                'semantic_hint_text': " ".join([filename, source_type] + tags).strip(),
                'width': width,
                'height': height,
                'is_vertical': (height >= width) if width and height else False,
                'emotion_strength': _infer_material_emotion(tags, filename),
            })
        except Exception as e:
            _report_runtime_message(
                "collect_video_files",
                f"读取素材失败: {filepath}",
                level="WARN",
                payload={"filepath": filepath},
                exc=e,
            )

    return videos


def _probe_media_duration(filepath: str) -> float:
    """用 ffprobe/ffmpeg 获取媒体时长，兼容视频与纯音频输入。"""
    ffprobe_error: Optional[Exception] = None
    try:
        result = run_hidden(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', filepath],
            capture_output=True, text=True, check=True, encoding='utf-8', errors='ignore'
        )
        duration = float(result.stdout.strip())
        if duration > 0:
            return duration
    except Exception as exc:
        ffprobe_error = exc

    ffmpeg_error: Optional[Exception] = None
    try:
        result = run_hidden(
            ['ffmpeg', '-i', filepath, '-f', 'null', '-'],
            capture_output=True, text=True, encoding='utf-8', errors='ignore'
        )
        duration_match = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.\d+)', result.stderr)
        if duration_match:
            h, m, s = duration_match.groups()
            return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception as exc:
        ffmpeg_error = exc

    if ffprobe_error or ffmpeg_error:
        _append_runtime_issue(
            "probe_media_duration",
            "ffprobe 和 ffmpeg 都未能直接解析媒体时长，已回退到剪映素材读取。",
            payload={
                "filepath": filepath,
                "ffprobe_error": f"{type(ffprobe_error).__name__}: {ffprobe_error}" if ffprobe_error else "",
                "ffmpeg_error": f"{type(ffmpeg_error).__name__}: {ffmpeg_error}" if ffmpeg_error else "",
            },
        )

    media = draft.VideoMaterial(filepath)
    return media.duration / 1_000_000.0


def _get_broll_strategy_config(sensitivity: str) -> Dict[str, float]:
    config_map = {
        "medium": {"min_gap": MIN_HOST_DURATION, "min_duration": 1.2, "max_duration": 3.8, "long_block_threshold": 5.0, "dense_block_threshold": 3.8},
        "high": {"min_gap": MIN_HOST_DURATION, "min_duration": 1.0, "max_duration": 4.0, "long_block_threshold": 4.2, "dense_block_threshold": 3.0},
    }
    
    config = config_map.get(sensitivity, config_map["medium"]).copy()
    
    if RHYTHM_MODE == "紧凑高频":
        config["min_duration"] = max(0.8, config["min_duration"] - 0.3)
        config["max_duration"] = 2.5
        config["min_gap"] = max(0.5, MIN_HOST_DURATION * 0.8)
    elif RHYTHM_MODE == "舒缓留白":
        config["min_duration"] = 2.0
        config["max_duration"] = 5.0
        config["min_gap"] = max(2.0, MIN_HOST_DURATION * 1.5)

    config["min_duration"] = max(0.8, float(INSERT_MIN_DURATION))
    config["max_duration"] = max(config["min_duration"], float(INSERT_MAX_DURATION))
    return config


def _fit_broll_candidate_to_block(
    block: Dict,
    desired_start: float,
    desired_end: float,
    last_end_time: float,
    strategy: Dict[str, float],
    video_duration: float,
) -> Optional[Tuple[float, float]]:
    min_gap = strategy["min_gap"]
    min_duration = strategy["min_duration"]
    max_duration = strategy["max_duration"]

    block_start = max(0.0, float(block["start"]))
    block_end = min(video_duration, float(block["end"]))
    if block_end - block_start < min_duration:
        return None

    start = max(block_start, float(desired_start))
    end = min(block_end, float(desired_end))
    if end <= start:
        end = min(block_end, start + min_duration)

    duration = min(max_duration, max(min_duration, end - start))
    earliest_start = 0.0 if last_end_time < 0 else last_end_time + min_gap
    start = max(start, earliest_start)
    end = min(block_end, start + duration)

    if end - start < min_duration:
        end = block_end
        start = max(block_start, end - min_duration)
        start = max(start, earliest_start)
        end = min(block_end, start + max_duration)

    if end - start < min_duration:
        return None

    return round(start, 3), round(end, 3)


def _material_type_label(semantic_type: str) -> str:
    return "产品展示" if semantic_type == "product" else "病症困扰"


def _enrich_candidate_semantics(candidates: List[Dict]) -> List[Dict]:
    ordered = sorted((dict(item) for item in candidates), key=lambda item: float(item.get("start_time", 0.0)))
    for idx, candidate in enumerate(ordered):
        prev_text = ordered[idx - 1].get("text", "") if idx > 0 else ""
        next_text = ordered[idx + 1].get("text", "") if idx + 1 < len(ordered) else ""
        profile = build_segment_semantic_script(
            text=str(candidate.get("text", "")),
            semantic_type=str(candidate.get("semantic_type", "")),
            tags=candidate.get("tags", []) or [],
            emotion_strength=str(candidate.get("emotion_strength", "medium")),
            context_prev=str(prev_text),
            context_next=str(next_text),
            trigger_reason=str(candidate.get("trigger_reason", "semantic")),
            intent=str(candidate.get("intent", "")),
            action_type=str(candidate.get("action_type", "")),
            scene_hint=str(candidate.get("scene_hint", "")),
            entities=candidate.get("entities", []) or [],
        )
        candidate["context_prev"] = prev_text
        candidate["context_next"] = next_text
        candidate["semantic_profile"] = profile
        candidate["intent"] = profile.get("intent", "")
        candidate["action_type"] = profile.get("action_type", "")
        candidate["scene_hint"] = profile.get("scene_hint", "")
        candidate["entities"] = profile.get("entities", [])
        candidate["visual_need"] = profile.get("visual_need", "")
    return ordered


def _select_material_from_ranked_pool(
    ranked_pool: List[Dict],
    *,
    threshold: float,
    tracker: UsageTracker,
    used_hashes: set,
    usage_history: Optional[BrollUsageHistory],
    allow_recent_reuse: bool,
    enforce_recent_cooldown: bool,
    batch_tracker: Optional[UsageTracker] = None,
) -> Tuple[Optional[Dict], float, Dict]:
    def _passes_filters(item: Dict) -> bool:
        score = float(item.get("score", 0.0))
        material = item.get("material") or {}
        if score < threshold:
            return False
        content_hash = str(material.get("content_hash", ""))
        if content_hash and content_hash in used_hashes:
            return False
        if tracker and not tracker.can_use(material, "broll"):
            return False
        if (
            usage_history
            and content_hash
            and enforce_recent_cooldown
            and not allow_recent_reuse
            and usage_history.is_recently_used(content_hash)
        ):
            return False
        return True

    # Pass 1: prefer materials never used in this batch
    for item in ranked_pool:
        material = item.get("material") or {}
        if not _passes_filters(item):
            continue
        if batch_tracker is not None and batch_tracker.get_count(material) > 0:
            continue
        return material, float(item.get("score", 0.0)), dict(item.get("score_breakdown") or {})

    # Pass 2: allow materials used once (max 2 per batch)
    for item in ranked_pool:
        material = item.get("material") or {}
        if not _passes_filters(item):
            continue
        if batch_tracker is not None and not batch_tracker.can_use(material, "broll"):
            continue
        return material, float(item.get("score", 0.0)), dict(item.get("score_breakdown") or {})

    return None, 0.0, {}


def _resolve_primary_thresholds(candidate: Dict) -> List[Tuple[str, float]]:
    base_threshold = float(SEMANTIC_MATCH_THRESHOLD)
    intent = str(candidate.get("intent", "")).strip()
    relaxed_threshold = base_threshold

    if intent == "symptom_scenario":
        relaxed_threshold = min(relaxed_threshold, 0.63)
    elif intent == "product_effect":
        relaxed_threshold = min(relaxed_threshold, 0.60)
    elif intent == "product_usage":
        relaxed_threshold = min(relaxed_threshold, 0.50)

    thresholds: List[Tuple[str, float]] = [("semantic_primary", base_threshold)]
    if relaxed_threshold < base_threshold:
        thresholds.append(("semantic_primary_relaxed", relaxed_threshold))
    return thresholds


def _resolve_emergency_threshold() -> float:
    threshold = float(GENERIC_EMERGENCY_THRESHOLD)
    if STRICT_SEMANTIC_INSERTION:
        threshold = max(threshold, SEMANTIC_REUSE_THRESHOLD, 0.58)
    return min(0.95, max(0.0, threshold))


def _classify_match_quality(fallback_level: str, semantic_score: float, is_shot_material: bool) -> Tuple[str, str]:
    level = str(fallback_level or "").strip().lower()
    score = float(semantic_score or 0.0)
    if level in ("blind_fallback", "rejected"):
        return "fallback", "盲选兜底"
    if "emergency" in level:
        return "degraded", "泛素材降级"
    if "previous_video" in level:
        return "degraded", "历史复用降级"
    if "relaxed" in level:
        return "relaxed", "放宽阈值命中"
    if score >= 0.86:
        return "strong", "强语义命中"
    if is_shot_material and score >= 0.72:
        return "strong", "镜头强命中"
    return "normal", "标准语义命中"


def _build_shot_recall_bundle(materials: List[Dict], role: str) -> Dict[str, object]:
    if not ENABLE_SHOT_RECALL or not materials:
        return {"materials": [], "index": None, "errors": [], "backend": "disabled"}

    llm_api_key = os.environ.get("LLM_API_KEY", "").strip()
    llm_base_url = os.environ.get("LLM_BASE_URL", "").strip()
    llm_model = os.environ.get("LLM_MODEL", "deepseek-ai/DeepSeek-V4-Flash").strip()
    vlm_model = SHOT_RECALL_VLM_MODEL.strip() or llm_model
    use_vlm = bool(SHOT_RECALL_USE_VLM and llm_api_key and vlm_model)
    max_videos = SHOT_RECALL_MAX_VIDEOS if SHOT_RECALL_MAX_VIDEOS > 0 else None
    if max_videos is not None:
        cache_materials = materials[:max_videos]
    else:
        cache_materials = materials

    cache_key = (
        role,
        bool(use_vlm),
        vlm_model if use_vlm else "",
        max_videos or 0,
        tuple(
            (
                str(item.get("path", "")),
                str(item.get("content_hash", "")),
                str(item.get("semantic_type", "")),
                round(float(item.get("visual_quality", 0.0) or 0.0), 4),
            )
            for item in cache_materials
        ),
    )
    if not SHOT_RECALL_FORCE_REFRESH:
        with _SHOT_RECALL_BUNDLE_CACHE_LOCK:
            cached = _SHOT_RECALL_BUNDLE_CACHE.get(cache_key)
        if cached is not None:
            return cached

    shot_library = build_shot_material_library(
        materials,
        role=role,
        use_vlm=use_vlm,
        api_key=llm_api_key,
        vlm_model=vlm_model,
        base_url=llm_base_url,
        force_refresh=SHOT_RECALL_FORCE_REFRESH,
        max_videos=max_videos,
    )
    recall_index = build_recall_index(shot_library.get("materials", []) or [])
    result = {
        "materials": shot_library.get("materials", []) or [],
        "index": recall_index,
        "errors": shot_library.get("errors", []) or [],
        "backend": recall_index.get("backend", "none"),
    }
    if not SHOT_RECALL_FORCE_REFRESH:
        with _SHOT_RECALL_BUNDLE_CACHE_LOCK:
            _SHOT_RECALL_BUNDLE_CACHE[cache_key] = result
    return result


def _pick_semantic_material(
    candidate: Dict,
    product_videos: List[Dict],
    symptom_videos: List[Dict],
    emergency_videos: List[Dict],
    previous_video_videos: List[Dict],
    primary_shot_bundle: Optional[Dict[str, object]],
    tracker: UsageTracker,
    used_hashes: set,
    usage_history: Optional[BrollUsageHistory],
    batch_tracker: Optional[UsageTracker] = None,
) -> Tuple[Optional[Dict], Optional[str], float, str, Dict]:
    semantic_type = candidate.get("semantic_type", "symptom")
    material_type = _material_type_label(semantic_type)
    primary_pool = product_videos if semantic_type == "product" else symptom_videos
    previous_pool = [
        item for item in previous_video_videos
        if item.get("semantic_type") in (semantic_type, "generic")
    ]
    emergency_pool = [
        item for item in emergency_videos
        if item.get("semantic_type") in (semantic_type, "generic")
    ]

    ranked_primary = rank_materials_by_semantics(candidate, primary_pool)
    ranked_previous = rank_materials_by_semantics(candidate, previous_pool)
    ranked_emergency = rank_materials_by_semantics(candidate, emergency_pool)

    if primary_shot_bundle and primary_shot_bundle.get("materials"):
        ranked_primary_shots = search_shot_materials(
            candidate,
            primary_shot_bundle.get("index"),
            top_n=SHOT_RECALL_TOP_N,
        )
        for primary_level, primary_threshold in _resolve_primary_thresholds(candidate):
            material, score, score_breakdown = _select_material_from_ranked_pool(
                ranked_primary_shots,
                threshold=primary_threshold,
                tracker=tracker,
                used_hashes=used_hashes,
                usage_history=usage_history,
                allow_recent_reuse=False,
                enforce_recent_cooldown=False,
                batch_tracker=batch_tracker,
            )
            if material:
                return material, material_type, score, f"{primary_level}_shot", score_breakdown

    for primary_level, primary_threshold in _resolve_primary_thresholds(candidate):
        material, score, score_breakdown = _select_material_from_ranked_pool(
            ranked_primary,
            threshold=primary_threshold,
            tracker=tracker,
            used_hashes=used_hashes,
            usage_history=usage_history,
            allow_recent_reuse=False,
            enforce_recent_cooldown=False,
            batch_tracker=batch_tracker,
        )
        if material:
            return material, material_type, score, primary_level, score_breakdown

    material, score, score_breakdown = _select_material_from_ranked_pool(
        ranked_previous,
        threshold=SEMANTIC_REUSE_THRESHOLD,
        tracker=tracker,
        used_hashes=used_hashes,
        usage_history=usage_history,
        allow_recent_reuse=ALLOW_PREVIOUS_VIDEO_REUSE_WITHIN_24H,
        enforce_recent_cooldown=True,
        batch_tracker=batch_tracker,
    )
    if material:
        return material, material_type, score, "previous_video_reuse", score_breakdown

    material, score, score_breakdown = _select_material_from_ranked_pool(
        ranked_emergency,
        threshold=_resolve_emergency_threshold(),
        tracker=tracker,
        used_hashes=used_hashes,
        usage_history=usage_history,
        allow_recent_reuse=False,
        enforce_recent_cooldown=False,
        batch_tracker=batch_tracker,
    )
    if material:
        return material, material_type, score, "emergency_generic", score_breakdown

    # 默认严格模式下禁用盲选兜底，避免为凑占比乱插不相关素材。
    if primary_pool and (ALLOW_BLIND_FALLBACK or not STRICT_SEMANTIC_INSERTION):
        available = [
            item for item in primary_pool
            if str(item.get("content_hash", "")) not in used_hashes
        ]
        if not available:
            available = primary_pool
        if tracker:
            trackable = [item for item in available if tracker.can_use(item, "broll")]
            if trackable:
                available = trackable
        if available:
            material = random.choice(available)
            return material, material_type, 0.0, "blind_fallback", {}

    return None, None, 0.0, "rejected", {}


def _materialize_broll_candidates(
    candidates: List[Dict],
    product_videos: List[Dict],
    symptom_videos: List[Dict],
    tracker: UsageTracker,
    video_id: str = "",
    decision_log_path: Optional[str] = None,
    batch_tracker: Optional[UsageTracker] = None,
) -> List[Dict]:
    matches: List[Dict] = []
    usage_history = BrollUsageHistory(BROLL_USAGE_HISTORY_PATH)
    current_used_hashes = set()
    candidates = _enrich_candidate_semantics(candidates)
    use_vlm_active = bool(
        SHOT_RECALL_USE_VLM
        and os.environ.get("LLM_API_KEY", "").strip()
        and (SHOT_RECALL_VLM_MODEL.strip() or os.environ.get("LLM_MODEL", "").strip())
    )

    # 以来源文件夹为准打标签，确保产品/病症素材不会被关键词推断误分类
    for v in product_videos:
        v["_source_type"] = "product"
    for v in symptom_videos:
        v["_source_type"] = "symptom"

    all_materials = build_material_semantic_library(product_videos + symptom_videos)
    library_materials = all_materials["materials"]
    emergency_pool = all_materials["emergency_pool"]
    product_library = [item for item in library_materials if item.get("semantic_type") == "product"]
    symptom_library = [item for item in library_materials if item.get("semantic_type") == "symptom"]
    previous_video_pool = usage_history.get_previous_video_materials(video_id, library_materials)
    product_shot_bundle = _build_shot_recall_bundle(product_library, role="product")
    symptom_shot_bundle = _build_shot_recall_bundle(symptom_library, role="symptom")

    if decision_log_path:
        _append_decision_log(
            decision_log_path,
            {
                "event": "shot_recall_initialized",
                "enabled": ENABLE_SHOT_RECALL,
                "use_vlm_requested": SHOT_RECALL_USE_VLM,
                "use_vlm_active": use_vlm_active,
                "force_refresh": SHOT_RECALL_FORCE_REFRESH,
                "vlm_model": SHOT_RECALL_VLM_MODEL.strip() or os.environ.get("LLM_MODEL", "").strip(),
                "top_n": SHOT_RECALL_TOP_N,
                "max_videos_per_pool": SHOT_RECALL_MAX_VIDEOS,
                "product_shot_count": len(product_shot_bundle.get("materials", []) or []),
                "symptom_shot_count": len(symptom_shot_bundle.get("materials", []) or []),
                "product_backend": product_shot_bundle.get("backend", "none"),
                "symptom_backend": symptom_shot_bundle.get("backend", "none"),
                "errors": (product_shot_bundle.get("errors", []) or [])[:8] + (symptom_shot_bundle.get("errors", []) or [])[:8],
                "strict_semantic_insertion": STRICT_SEMANTIC_INSERTION,
                "allow_blind_fallback": ALLOW_BLIND_FALLBACK,
                "generic_emergency_threshold": _resolve_emergency_threshold(),
            },
        )

    for candidate in candidates:
        primary_shot_bundle = product_shot_bundle if candidate.get("semantic_type") == "product" else symptom_shot_bundle
        material, material_type, semantic_score, fallback_level, score_breakdown = _pick_semantic_material(
            candidate=candidate,
            product_videos=product_library,
            symptom_videos=symptom_library,
            emergency_videos=emergency_pool,
            previous_video_videos=previous_video_pool,
            primary_shot_bundle=primary_shot_bundle,
            tracker=tracker,
            used_hashes=current_used_hashes,
            usage_history=usage_history,
            batch_tracker=batch_tracker,
        )
        if not material:
            if decision_log_path:
                _append_decision_log(
                    decision_log_path,
                    {
                        "event": "broll_material_rejected",
                        "semantic_type": candidate.get("semantic_type"),
                        "text": candidate.get("text", ""),
                        "intent": candidate.get("intent", ""),
                        "action_type": candidate.get("action_type", ""),
                        "scene_hint": candidate.get("scene_hint", ""),
                        "reason": "no_material_meets_semantic_threshold",
                        "threshold_primary": SEMANTIC_MATCH_THRESHOLD,
                        "threshold_cascade": SEMANTIC_REUSE_THRESHOLD,
                    },
                )
            continue

        if tracker:
            tracker.record(material)
        if batch_tracker is not None:
            batch_tracker.record(material)
        if material.get("content_hash"):
            current_used_hashes.add(material["content_hash"])
        usage_history.record_use(video_id or "unknown_video", material, semantic_score, fallback_level)

        matched_item = {
            "start_time": candidate["start_time"],
            "end_time": candidate["end_time"],
            "duration": candidate["duration"],
            "material": material,
            "material_type": material_type,
            "text": candidate.get("text", "语义中插"),
            "is_transition": candidate.get("is_transition", False),
            "semantic_type": candidate["semantic_type"],
            "intent": candidate.get("intent", ""),
            "action_type": candidate.get("action_type", ""),
            "scene_hint": candidate.get("scene_hint", ""),
            "entities": candidate.get("entities", []),
            "visual_need": candidate.get("visual_need", ""),
            "trigger_reason": candidate.get("trigger_reason", "semantic"),
            "trigger_keyword": candidate.get("trigger_keyword"),
            "emotion_strength": candidate.get("emotion_strength", "medium"),
            "semantic_score": semantic_score,
            "fallback_level": fallback_level,
            "score_breakdown": score_breakdown,
            "content_hash": material.get("content_hash", ""),
            "source_start_us": material.get("source_start_us"),
            "duration_us": material.get("duration_us"),
            "is_shot_material": bool(material.get("is_shot_material")),
            "shot_index": material.get("shot_index"),
            "shot_start_seconds": material.get("shot_start_seconds"),
            "shot_end_seconds": material.get("shot_end_seconds"),
            "shot_thumbnail_path": material.get("thumbnail_path", ""),
            "shot_caption": material.get("caption", ""),
        }
        match_quality, match_quality_label = _classify_match_quality(
            fallback_level,
            semantic_score,
            bool(matched_item.get("is_shot_material")),
        )
        matched_item["match_quality"] = match_quality
        matched_item["match_quality_label"] = match_quality_label
        matches.append(matched_item)
        if decision_log_path:
            _append_decision_log(
                decision_log_path,
                {
                    "event": "broll_material_selected",
                    "semantic_type": matched_item["semantic_type"],
                    "trigger_reason": matched_item["trigger_reason"],
                    "trigger_keyword": matched_item.get("trigger_keyword"),
                    "start_time": matched_item["start_time"],
                    "end_time": matched_item["end_time"],
                    "material_name": material["filename"],
                    "material_path": material["path"],
                    "material_tags": material.get("tags", []),
                    "candidate_intent": matched_item.get("intent"),
                    "candidate_action_type": matched_item.get("action_type"),
                    "candidate_scene_hint": matched_item.get("scene_hint"),
                    "candidate_entities": matched_item.get("entities", []),
                    "semantic_score": semantic_score,
                    "score_breakdown": score_breakdown,
                    "fallback_level": fallback_level,
                    "match_quality": match_quality,
                    "match_quality_label": match_quality_label,
                    "is_shot_material": matched_item.get("is_shot_material", False),
                    "shot_index": matched_item.get("shot_index"),
                    "shot_start_seconds": matched_item.get("shot_start_seconds"),
                    "shot_end_seconds": matched_item.get("shot_end_seconds"),
                    "shot_caption": matched_item.get("shot_caption", ""),
                },
            )

    return matches


def _preferred_semantic_sequence(total_slots: int, ratio_text: str) -> List[str]:
    symptom_ratio, product_ratio = _parse_ratio_config(ratio_text)
    sequence = (["symptom"] * symptom_ratio) + (["product"] * product_ratio)
    if not sequence:
        sequence = ["symptom", "product"]
    return [sequence[idx % len(sequence)] for idx in range(max(1, total_slots))]


def _select_user_bgm_file(user_bgm_dir: str) -> str:
    target_dir = user_bgm_dir or USER_BGM_DIR
    if not target_dir or not os.path.isdir(target_dir):
        raise RuntimeError("未检测到用户背景音乐文件夹，请检查./my_bg_music/路径")

    bgm_files: List[str] = []
    for ext in ("*.wav", "*.mp3", "*.m4a", "*.aac"):
        bgm_files.extend(glob.glob(os.path.join(target_dir, ext)))
    bgm_files = [path for path in bgm_files if os.path.isfile(path)]
    if not bgm_files:
        raise RuntimeError("未检测到用户背景音乐文件夹，请检查./my_bg_music/路径")

    bgm_files.sort(key=lambda path: os.path.basename(path).lower())
    if BGM_PICK_MODE == "随机洗牌":
        random.shuffle(bgm_files)
    return bgm_files[0]

def _build_time_based_broll_candidates(
    video_duration: float,
    sensitivity: str,
    insert_min_duration: float,
    insert_max_duration: float,
    video_id: str = "",
) -> List[Dict]:
    """纯时间驱动：基于视频指纹生成可复现但不完全一致的中插节奏骨架"""
    strategy = _get_broll_strategy_config(sensitivity)
    if video_duration <= 0:
        return []

    candidates: List[Dict] = []
    seed_source = f"{video_id}|{video_duration:.3f}|{sensitivity}|{insert_min_duration:.2f}|{insert_max_duration:.2f}"
    seed = int(hashlib.md5(seed_source.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)

    base_window_span = max(4.2, float(strategy.get("long_block_threshold", 5.0)))
    lead_guard = min(6.0, max(insert_min_duration * 1.2, video_duration * 0.04))
    tail_guard = min(5.0, max(insert_min_duration, video_duration * 0.03))
    usable_start = min(video_duration * 0.25, lead_guard)
    usable_end = max(usable_start + insert_min_duration, video_duration - tail_guard)
    usable_duration = max(insert_min_duration * 2, usable_end - usable_start)

    estimated_windows = usable_duration / base_window_span
    total_windows = max(1, int(round(estimated_windows + rng.uniform(-0.45, 0.65))))
    if video_duration >= 45:
        total_windows = max(2, total_windows)

    preferred_types = _preferred_semantic_sequence(total_windows * 3, BROLL_RATIO)
    sequence_offset = seed % max(1, len(preferred_types))
    candidate_templates = {
        "symptom": [
            {
                "text": "夜间皮肤瘙痒抓挠特写",
                "tags": ["瘙痒", "抓挠", "皮肤", "特写", "夜间"],
                "intent": "symptom_feeling",
                "action_type": "scratch_relief",
                "scene_hint": "night_home",
                "entities": ["瘙痒", "皮肤", "睡眠"],
            },
            {
                "text": "皮肤泛红红斑局部展示",
                "tags": ["泛红", "红斑", "皮肤", "局部", "特写"],
                "intent": "symptom_appearance",
                "action_type": "skin_closeup",
                "scene_hint": "generic_scene",
                "entities": ["泛红", "皮肤"],
            },
            {
                "text": "日常场景反复发痒困扰",
                "tags": ["瘙痒", "困扰", "日常", "场景"],
                "intent": "symptom_scenario",
                "action_type": "daily_life_scene",
                "scene_hint": "daily_home",
                "entities": ["瘙痒", "社交"],
            },
        ],
        "product": [
            {
                "text": "药膏均匀涂抹使用演示",
                "tags": ["药膏", "涂抹", "使用", "皮肤特写"],
                "intent": "product_usage",
                "action_type": "apply_ointment",
                "scene_hint": "bathroom_usage",
                "entities": ["乳膏", "涂抹"],
            },
            {
                "text": "产品包装外盒展示特写",
                "tags": ["产品", "包装", "外盒", "特写"],
                "intent": "product_form",
                "action_type": "packshot_display",
                "scene_hint": "generic_scene",
                "entities": ["产品"],
            },
            {
                "text": "缓解瘙痒的产品效果展示",
                "tags": ["缓解", "止痒", "产品", "效果"],
                "intent": "product_effect",
                "action_type": "apply_ointment",
                "scene_hint": "generic_scene",
                "entities": ["瘙痒", "产品"],
            },
        ],
    }

    window_weights = [0.82 + rng.random() * 0.55 for _ in range(total_windows)]
    weight_sum = sum(window_weights) or 1.0
    normalized_weights = [weight / weight_sum for weight in window_weights]
    cursor = usable_start

    for idx in range(total_windows):
        window_span = usable_duration * normalized_weights[idx]
        window_start = cursor
        if idx == total_windows - 1:
            window_end = usable_end
        else:
            jitter = rng.uniform(-0.22, 0.18) * min(base_window_span, window_span)
            window_end = min(usable_end, max(window_start + insert_min_duration, cursor + window_span + jitter))
        cursor = window_end

        semantic_type = preferred_types[(idx + sequence_offset) % len(preferred_types)]
        template_pool = candidate_templates.get(semantic_type, []) or candidate_templates["symptom"]
        template_offset = (seed // 7) % len(template_pool)
        template = template_pool[(idx + template_offset) % len(template_pool)]

        duration_ratio = 0.46 + rng.random() * 0.26
        desired_duration = min(insert_max_duration, max(insert_min_duration, (window_end - window_start) * duration_ratio))
        placement_ratio = min(0.78, max(0.14, [0.22, 0.38, 0.52, 0.68][(idx + seed) % 4] + rng.uniform(-0.08, 0.08)))
        start = window_start + max(0.0, (window_end - window_start - desired_duration)) * placement_ratio
        end = min(video_duration, start + desired_duration)
        if end - start < insert_min_duration:
            continue
        candidates.append({
            "start_time": round(start, 3),
            "end_time": round(end, 3),
            "duration": round(end - start, 3),
            "semantic_type": semantic_type,
            "text": template["text"],
            "is_transition": False,
            "is_density_fill": True,
            "trigger_reason": "time_based_varied",
            "emotion_strength": "medium",
            "tags": list(template.get("tags", [])),
            "intent": str(template.get("intent", "")),
            "action_type": str(template.get("action_type", "")),
            "scene_hint": str(template.get("scene_hint", "")),
            "entities": list(template.get("entities", [])),
        })

    return candidates


def smart_material_matching(
    video_duration: float,
    product_videos: List[Dict],
    symptom_videos: List[Dict],
    sensitivity: str = 'medium',
    video_id: str = "default_video",
    tracker: UsageTracker = None,
    batch_tracker: Optional[UsageTracker] = None,
    speech_video_path: str = "",
) -> Tuple[List[Dict], List[Dict], str]:
    """纯时间驱动 + 密度自动补齐至 65% 的智能素材匹配"""
    print(f"正在进行智能素材匹配 (时间驱动版)...")

    if video_duration <= 0:
        print("   ⚠ 无效的视频时长，无法匹配素材")
        return [], [], "neutral"

    llm_api_key = os.environ.get("LLM_API_KEY", "").strip()
    llm_base_url = os.environ.get("LLM_BASE_URL", "").strip()
    llm_model = os.environ.get("LLM_MODEL", "deepseek-ai/DeepSeek-V4-Flash").strip()

    matches = []
    sfx_list = []
    bgm_emotion = "neutral"
    candidate_matches = []
    fill_actions: List[Dict] = []
    density_warning = None

    density_template = get_density_template_config(DENSITY_TEMPLATE)
    density_window_seconds = max(10.0, float(DENSITY_WINDOW_SECONDS or density_template["window_seconds"]))
    density_min_segments = max(1, int(MIN_INSERTS_PER_WINDOW or density_template["min_segments_per_window"]))
    insert_min_duration = max(0.8, float(INSERT_MIN_DURATION or density_template["insert_min_duration"]))
    insert_max_duration = max(insert_min_duration, float(INSERT_MAX_DURATION or density_template["insert_max_duration"]))
    decision_log_path = _decision_log_path(video_id)
    if os.path.exists(decision_log_path):
        os.remove(decision_log_path)

    product_pool_report = _validate_tagged_material_pool(product_videos, "产品展示池", "product")
    symptom_pool_report = _validate_tagged_material_pool(symptom_videos, "病症展示池", "symptom")
    _append_decision_log(decision_log_path, {"event": "pool_validation", "product_pool": product_pool_report, "symptom_pool": symptom_pool_report})

    # LLM 剧本生成 — 优先基于真实口播转写
    transcript_segments = None
    if speech_video_path:
        try:
            from speech_transcriber import transcribe_speech
            transcript_segments = transcribe_speech(
                video_path=speech_video_path,
                video_name=video_id,
                cache_dir=os.path.dirname(decision_log_path) if decision_log_path else OUTPUT_DIR,
            )
        except Exception as e:
            print(f"   [Whisper] 转写阶段异常，降级: {e}")

    if llm_api_key:
        try:
            import llm_clip_matcher
            if transcript_segments:
                plan = llm_clip_matcher.generate_editing_plan_with_transcript(
                    video_duration=video_duration,
                    transcript_segments=transcript_segments,
                    api_key=llm_api_key,
                    model=llm_model,
                    base_url=llm_base_url if llm_base_url else None,
                    density_config=density_template,
                )
            else:
                plan = llm_clip_matcher.generate_editing_plan_with_llm(
                    video_duration=video_duration,
                    api_key=llm_api_key,
                    model=llm_model,
                    base_url=llm_base_url if llm_base_url else None,
                    density_config=density_template,
                )
            if plan:
                print("   [LLM] 成功获取大模型语义剧本，开始组装素材...")
                for b in plan.get("b_rolls", []):
                    start_time = float(b.get("start", 0))
                    end_time = float(b.get("end", 0))
                    semantic_type = b.get("type", "symptom")
                    if end_time - start_time < 0.5:
                        continue
                    candidate_matches.append({
                        "start_time": start_time,
                        "end_time": end_time,
                        "duration": round(end_time - start_time, 3),
                        "semantic_type": semantic_type,
                        "text": b.get("reason", "LLM中插"),
                        "is_transition": False,
                        "trigger_reason": "llm_plan",
                        "emotion_strength": str(b.get("emotion_strength", "medium")),
                        "intent": b.get("intent", ""),
                        "action_type": b.get("action_type", ""),
                        "scene_hint": b.get("scene_hint", ""),
                        "entities": b.get("entities", []),
                    })
                sfx_list = plan.get("sfx", [])
                bgm_emotion = plan.get("bgm_emotion", "neutral")
        except Exception as e:
            print(f"   [LLM Error] 大模型处理异常: {e}，回退到时间驱动匹配。")

    # 时间驱动中插：等分时间窗口，交替分配语义类型
    time_based_candidates = _build_time_based_broll_candidates(
        video_duration=video_duration,
        sensitivity=sensitivity,
        insert_min_duration=insert_min_duration,
        insert_max_duration=insert_max_duration,
        video_id=video_id,
    )

    if not candidate_matches and transcript_segments:
        try:
            import llm_clip_matcher
            keyword_plan = llm_clip_matcher.plan_insertions_with_keywords(
                transcript_segments=transcript_segments,
                video_duration=video_duration,
                density_config=density_template,
            )
            if keyword_plan and keyword_plan.get("b_rolls"):
                print("   [INFO] 使用关键词规则基于口播转写规划中插...")
                for b in keyword_plan["b_rolls"]:
                    start_time = float(b.get("start", 0))
                    end_time = float(b.get("end", 0))
                    semantic_type = b.get("type", "symptom")
                    if end_time - start_time < 0.5:
                        continue
                    candidate_matches.append({
                        "start_time": start_time,
                        "end_time": end_time,
                        "duration": round(end_time - start_time, 3),
                        "semantic_type": semantic_type,
                        "text": b.get("reason", "关键词中插"),
                        "is_transition": False,
                        "trigger_reason": "keyword_plan",
                        "emotion_strength": "medium",
                        "intent": "",
                        "action_type": "",
                        "scene_hint": "",
                        "entities": [],
                    })
        except Exception as e:
            print(f"   [WARN] 关键词规则规划失败: {e}")

    if not candidate_matches:
        print("   [INFO] 使用时间驱动策略规划中插...")
        candidate_matches = time_based_candidates
    else:
        llm_total = sum(float(c.get("duration", 0.0)) for c in candidate_matches)
        llm_ratio = llm_total / video_duration if video_duration > 0 else 0.0
        if llm_ratio < TARGET_BROLL_COVERAGE_RATIO:
            print(f"   [INFO] LLM 中插占比 {llm_ratio:.1%} 不足目标 {TARGET_BROLL_COVERAGE_RATIO:.0%}，补充时间驱动候选...")
            existing_spans = [(float(c["start_time"]), float(c["end_time"])) for c in candidate_matches]
            for tc in time_based_candidates:
                tc_start = float(tc["start_time"])
                tc_end = float(tc["end_time"])
                conflict = False
                for es, ee in existing_spans:
                    if min(tc_end, ee) - max(tc_start, es) > 0.5:
                        conflict = True
                        break
                if not conflict:
                    candidate_matches.append(tc)
                    existing_spans.append((tc_start, tc_end))
            candidate_matches.sort(key=lambda x: float(x["start_time"]))

    density_windows = analyze_insert_density(
        candidate_matches,
        video_duration=video_duration,
        window_seconds=density_window_seconds,
        min_segments_per_window=density_min_segments,
    )
    pending_windows = [item for item in density_windows if item["status"] != "达标"]
    if pending_windows:
        _report_runtime_message(
            "density_analysis",
            f"发现 {len(pending_windows)} 个时间窗口中插密度不足",
            level="WARN",
            payload={"pending_window_count": len(pending_windows), "video_id": video_id},
        )

    if AUTO_FILL_DENSITY:
        candidate_matches, density_windows, fill_actions = auto_fill_density_gaps(
            candidate_matches=candidate_matches,
            video_duration=video_duration,
            window_seconds=density_window_seconds,
            min_segments_per_window=density_min_segments,
            insert_min_duration=insert_min_duration,
            insert_max_duration=insert_max_duration,
            target_ratio=TARGET_BROLL_COVERAGE_RATIO,
        )
        if fill_actions:
            print(f"   [OK] 已自动补齐 {len(fill_actions)} 处中插建议")

    print("   [INFO] 根据语义块校准产品与病症中插位置，并避免连续中插...")
    matches = _materialize_broll_candidates(
        candidates=candidate_matches,
        product_videos=product_videos,
        symptom_videos=symptom_videos,
        tracker=tracker,
        video_id=video_id,
        decision_log_path=decision_log_path,
        batch_tracker=batch_tracker,
    )
    matches.sort(key=lambda x: x["start_time"])

    density_windows = analyze_insert_density(
        matches,
        video_duration=video_duration,
        window_seconds=density_window_seconds,
        min_segments_per_window=density_min_segments,
    )
    unresolved_density = [item for item in density_windows if item["status"] != "达标"]
    if unresolved_density:
        print(
            f"   [WARN] 真实素材落地后仍有 {len(unresolved_density)} 个窗口未达标，"
            "尝试执行二次密度补齐..."
        )
        refill_candidates, _, refill_actions = auto_fill_density_gaps(
            candidate_matches=matches,
            video_duration=video_duration,
            window_seconds=density_window_seconds,
            min_segments_per_window=density_min_segments,
            insert_min_duration=insert_min_duration,
            insert_max_duration=insert_max_duration,
            target_ratio=TARGET_BROLL_COVERAGE_RATIO,
        )
        if refill_actions:
            refill_matches = _materialize_broll_candidates(
                candidates=refill_candidates,
                product_videos=product_videos,
                symptom_videos=symptom_videos,
                tracker=None,
                video_id=video_id,
                decision_log_path=decision_log_path,
                batch_tracker=batch_tracker,
            )
            refill_matches.sort(key=lambda x: x["start_time"])
            original_ratio = sum(float(m.get("duration", 0.0)) for m in matches) / video_duration if video_duration > 0 else 0.0
            refill_ratio = sum(float(m.get("duration", 0.0)) for m in refill_matches) / video_duration if video_duration > 0 else 0.0
            if refill_ratio >= original_ratio:
                matches = refill_matches
                fill_actions.extend(refill_actions)
                density_windows = analyze_insert_density(
                    matches,
                    video_duration=video_duration,
                    window_seconds=density_window_seconds,
                    min_segments_per_window=density_min_segments,
                )
                unresolved_density = [item for item in density_windows if item["status"] != "达标"]

    if unresolved_density:
        density_warning = (
            f"中插密度未完全达标，仍有 {len(unresolved_density)} 个窗口低于 "
            f">={density_min_segments}条/{int(density_window_seconds)}秒 的标准；"
            "本次已降级为继续生成草稿并输出告警报告。"
        )
        _report_runtime_message(
            "density_analysis",
            density_warning,
            level="WARN",
            payload={"video_id": video_id, "unresolved_window_count": len(unresolved_density)},
        )

    output_dir = OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)
    export_density_reports(
        video_id=video_id,
        output_dir=output_dir,
        matches=matches,
        density_windows=density_windows,
        fill_actions=fill_actions,
    )

    json_path = os.path.join(output_dir, f"{video_id}_insert_density_config.json")
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(
            [
                {
                    'start': round(m['start_time'], 2),
                    'end': round(m['end_time'], 2),
                    'duration': round(m['duration'], 2),
                    'type': m['material_type'],
                    'auto_fill': bool(m.get('is_density_fill', False)),
                    'intent': m.get('intent', ''),
                    'action_type': m.get('action_type', ''),
                    'scene_hint': m.get('scene_hint', ''),
                    'semantic_score': round(float(m.get('semantic_score', 0.0)), 4),
                    'score_breakdown': m.get('score_breakdown', {}),
                    'fallback_level': m.get('fallback_level', 'semantic_primary'),
                    'match_quality': m.get('match_quality', 'normal'),
                    'match_quality_label': m.get('match_quality_label', '标准语义命中'),
                }
                for m in matches
            ],
            f,
            ensure_ascii=False,
            indent=2,
        )

    ratio_report = analyze_broll_ratio(
        matches,
        video_duration=video_duration,
        target_ratio=TARGET_BROLL_COVERAGE_RATIO,
    )
    ratio_report_path = os.path.join(output_dir, f"{video_id}_broll_ratio_report.json")
    with open(ratio_report_path, "w", encoding="utf-8") as f:
        json.dump(ratio_report, f, ensure_ascii=False, indent=2)
    if ratio_report["total_status"] != "达标":
        _report_runtime_message(
            "broll_ratio",
            f"中插总占比 {ratio_report['total_ratio']:.1%} 低于目标 {TARGET_BROLL_COVERAGE_RATIO:.0%}，已输出占比监控报告。",
            level="WARN",
            payload={"video_id": video_id, "total_ratio": ratio_report["total_ratio"], "target_ratio": TARGET_BROLL_COVERAGE_RATIO},
        )

    if density_warning:
        warning_path = os.path.join(output_dir, f"{video_id}_density_warning.txt")
        with open(warning_path, "w", encoding="utf-8") as f:
            f.write(density_warning)

    csv_path = os.path.join(output_dir, f"{video_id}_host_face_statistics.csv")
    final_insert = sum(m['duration'] for m in matches)
    face_duration = video_duration - final_insert
    final_ratio = face_duration / video_duration if video_duration > 0 else 0
    with open(csv_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['video_id', 'total_duration', 'host_face_duration', 'ratio'])
        writer.writerow([video_id, f"{video_duration:.2f}", f"{face_duration:.2f}", f"{final_ratio:.2%}"])

    return matches, sfx_list, bgm_emotion


def create_otc_promo_video(
    project_name: str,
    speech_video: str,
    matches: List[Dict],
    sfx_list: List[Dict] = None,
    bgm_emotion: str = "neutral",
    bgm_path: Optional[str] = None,
    tracker: UsageTracker = None,
    is_review_version: bool = False
) -> bool:
    """创建OTC药品推广视频"""
    try:
        print(f"\n正在创建OTC推广视频: {project_name}")
        decision_log_path = _decision_log_path(os.path.splitext(os.path.basename(speech_video))[0])
        symptom_matches = sum(1 for m in matches if m.get('semantic_type') == "symptom")
        product_matches = sum(1 for m in matches if m.get('semantic_type') == "product")
        is_valid_speech_video, validation_message = validate_speech_video_file(speech_video)
        if not is_valid_speech_video:
            print(validation_message)
            return False

        detected_version = detect_jianying_version()
        if detected_version and not is_supported_jianying_version(detected_version):
            _report_runtime_message(
                "jianying_version",
                f"检测到当前剪映版本为 {detected_version}，非 5.9 兼容版本。将继续生成草稿，但无法保证可直接在剪映中打开。",
                level="WARN",
                payload={"detected_version": detected_version},
            )
        if not detected_version:
            _report_runtime_message("jianying_version", "未能自动识别剪映版本，将继续尝试生成草稿。", level="WARN")
        else:
            print(f"   检测到剪映版本: {detected_version}")
        
        # 获取口播视频时长
        speech_duration = 0
        try:
            result = run_hidden(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', speech_video],
                capture_output=True, text=True, check=True, encoding='utf-8', errors='ignore'
            )
            speech_duration = float(result.stdout.strip())
        except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
            try:
                result = run_hidden(
                    ['ffmpeg', '-i', speech_video, '-f', 'null', '-'],
                    capture_output=True, text=True, encoding='utf-8', errors='ignore'
                )
                duration_match = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.\d+)', result.stderr)
                if duration_match:
                    h, m, s = duration_match.groups()
                    speech_duration = int(h) * 3600 + int(m) * 60 + float(s)
                else:
                    raise RuntimeError("无法从FFmpeg输出解析时长")
            except Exception as exc:
                _report_runtime_message(
                    "speech_duration_probe",
                    "无法通过 ffprobe 和 ffmpeg 获取视频时长，回退为 60 秒默认值。",
                    level="WARN",
                    payload={"speech_video": speech_video},
                    exc=exc,
                )
                _report_runtime_message(
                    "speech_duration_probe",
                    "中插密度、时间线排布和覆盖率计算可能不准确，请检查视频文件是否损坏。",
                    level="WARN",
                    payload={"speech_video": speech_video},
                )
                speech_duration = 60

        print(f"   口播视频时长: {speech_duration:.2f}秒")

        matches, broll_stats = sanitize_non_overlapping_segments(matches, speech_duration)
        if broll_stats["shifted_count"] or broll_stats["dropped_count"]:
            print(
                "   [监控] 中插时间线已自动净化: "
                f"调整 {broll_stats['shifted_count']} 条, 丢弃 {broll_stats['dropped_count']} 条"
            )
        
        draft_root = get_draft_root()
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        draft_lock_path = os.path.join(OUTPUT_DIR, ".otc_draft_write.lock")

        # 草稿目录和 root_meta_info.json 是共享状态，串行化写入可避免索引丢失与列表异常。
        with file_lock(draft_lock_path, timeout=180.0):
            # 创建项目（竖屏9:16），添加时间戳后缀避免文件占用冲突
            timestamp = datetime.now().strftime("%H%M%S")
            version_suffix = "_审查版" if is_review_version else "_干净版"
            unique_project_name = f"{project_name}{version_suffix}_{timestamp}"
            project = JyProject(
                unique_project_name,
                overwrite=True,
                width=1080,
                height=1920,
                drafts_root=draft_root,
            )

            if is_review_version:
                project.script.add_track(draft.TrackType.video, "07_Review_Watermark", absolute_index=30000)
                project.add_text_simple(
                    text="【审查版本】对齐与时码校验",
                    start_time="0s",
                    duration=f"{speech_duration}s",
                    track_name="07_Review_Watermark",
                    style=draft.TextStyle(size=10.0, color=(1.0, 0.0, 0.0)),
                    border=draft.TextBorder(color=(1.0, 1.0, 1.0), alpha=1.0, width=0.05),
                    clip_settings=draft.ClipSettings(transform_y=0.45)
                )

            # 1. 添加主轨道：口播视频
            print("   添加主轨道...")
            # 预先创建主视频轨道，保证其 index 最小 (最底层)
            project.script.add_track(draft.TrackType.video, "01_Main_Video", absolute_index=0)
            if os.path.exists(speech_video):
                project.add_media_safe(speech_video, start_time="0s", track_name="01_Main_Video")

            # 2. 添加B-Roll素材（中插视频），确保其在最底层（紧贴主视频）
            print("   添加中插素材...")
            project.script.add_track(draft.TrackType.video, "02_B_Roll", absolute_index=10)
            for match in matches:
                material = match['material']
                start_time = match['start_time']
                duration = match['duration']
                
                if start_time + duration > speech_duration:
                    duration = speech_duration - start_time
                    if duration <= 0:
                        continue
                
                start_us = match.get("start_us", seconds_to_microseconds(start_time))
                duration_us = match.get("duration_us", seconds_to_microseconds(duration))
                source_start_us = match.get("source_start_us")

                if match.get('is_placeholder', False):
                    seg = project.add_media_safe(
                        speech_video,
                        start_time=start_us,
                        duration=duration_us,
                        track_name="02_B_Roll",
                        source_start=start_us
                    )
                else:
                    add_media_kwargs = {
                        "start_time": start_us,
                        "duration": duration_us,
                        "track_name": "02_B_Roll",
                    }
                    if source_start_us is not None:
                        add_media_kwargs["source_start"] = source_start_us
                    seg = project.add_media_safe(material['path'], **add_media_kwargs)
                if seg and hasattr(seg, 'volume'):
                    seg.volume = 0.0
                if seg and hasattr(seg, "add_fade"):
                    try:
                        seg.add_fade("0.2s", "0.2s")
                    except Exception as exc:
                        _append_runtime_issue(
                            "timeline_broll_fade",
                            "中插素材淡入淡出添加失败，已继续生成。",
                            exc=exc,
                            payload={"material_path": material.get("path", ""), "track_name": "02_B_Roll"},
                        )

            if is_review_version:
                # 3. 添加广审素材轨道 (Ad Review)
                print("   添加广审素材轨道...")
                project.script.add_track(draft.TrackType.video, "05_Ad_Review", absolute_index=99999)
                try:
                    ad_added = False
                    local_ad_files = []
                    if AD_REVIEW_DIR and os.path.isdir(AD_REVIEW_DIR):
                        for ext in ('*.png', '*.jpg', '*.jpeg', '*.mp4', '*.mov'):
                            local_ad_files.extend(glob.glob(os.path.join(AD_REVIEW_DIR, ext)))

                    if tracker:
                        local_ad_files = tracker.filter_available(local_ad_files, "ad_review")

                    if local_ad_files:
                        chosen_ad = random.choice(local_ad_files)
                        if tracker:
                            tracker.record(chosen_ad)

                        ad_seg = project.add_media_safe(
                            chosen_ad,
                            start_time="0s",
                            duration=f"{speech_duration}s",
                            track_name="05_Ad_Review"
                        )
                        if ad_seg:
                            ad_seg.clip_settings = draft.ClipSettings(transform_y=0.0, scale_x=1.0, scale_y=1.0, alpha=1.0)
                            ad_added = True
                            print(f"   [OK] 使用广审素材: {os.path.basename(chosen_ad)}, 已设置100%全覆盖居中")
                    if not ad_added:
                        _report_runtime_message(
                            "timeline_ad_review",
                            "广审素材未添加（未找到有效文件或已达到使用上限）",
                            level="SKIP",
                            payload={"speech_video": speech_video, "ad_review_dir": AD_REVIEW_DIR},
                        )
                except Exception as e:
                    _report_runtime_message(
                        "timeline_ad_review",
                        f"广审轨道添加失败: {e}",
                        level="SKIP",
                        payload={"speech_video": speech_video, "ad_review_dir": AD_REVIEW_DIR},
                        exc=e,
                    )

                # 4. 添加顶部贴图素材 (Top Sticker)
                print("   添加顶部贴图素材...")
                project.script.add_track(draft.TrackType.video, "06_Top_Sticker", absolute_index=99998)
                try:
                    sticker_added = False
                    local_sticker_files = []
                    if STICKER_DIR and os.path.isdir(STICKER_DIR):
                        for ext in ('*.png', '*.jpg', '*.jpeg', '*.mp4', '*.mov'):
                            local_sticker_files.extend(glob.glob(os.path.join(STICKER_DIR, ext)))

                    if tracker:
                        local_sticker_files = tracker.filter_available(local_sticker_files, "sticker")

                    if local_sticker_files:
                        chosen_sticker = random.choice(local_sticker_files)
                        if tracker:
                            tracker.record(chosen_sticker)

                        sticker_seg = project.add_media_safe(
                            chosen_sticker,
                            start_time="0s",
                            duration=f"{speech_duration}s",
                            track_name="06_Top_Sticker"
                        )
                        if sticker_seg:
                            sticker_seg.clip_settings = draft.ClipSettings(transform_y=0.0, scale_x=1.0, scale_y=1.0, alpha=1.0)
                            sticker_added = True
                            print(f"   [OK] 使用贴图素材: {os.path.basename(chosen_sticker)}, 已设置100%全覆盖居中")
                    if not sticker_added:
                        _report_runtime_message(
                            "timeline_sticker",
                            "贴图素材未添加（未找到有效文件或已达到使用上限）",
                            level="SKIP",
                            payload={"speech_video": speech_video, "sticker_dir": STICKER_DIR},
                        )
                except Exception as e:
                    _report_runtime_message(
                        "timeline_sticker",
                        f"贴图添加失败: {e}",
                        level="SKIP",
                        payload={"speech_video": speech_video, "sticker_dir": STICKER_DIR},
                        exc=e,
                    )

            # 5. 添加背景音乐轨道（BGM）
            print(f"   添加背景音乐轨道 (强制用户素材)...")
            bgm_report = None
            try:
                bgm_added = False
                chosen_bgm = _select_user_bgm_file(bgm_path or USER_BGM_DIR)
                if os.path.abspath(chosen_bgm) == os.path.abspath(speech_video):
                    raise RuntimeError("检测到口播原音被误用为背景音乐，已终止并要求重新选择用户BGM")
                prepared_bgm_path, bgm_report = prepare_bgm_for_timeline(
                    bgm_path=chosen_bgm,
                    target_duration_sec=speech_duration,
                    output_dir=OUTPUT_DIR,
                    prefix=unique_project_name,
                    crossfade_ms=BGM_CROSSFADE_MS,
                    target_lufs=BGM_TARGET_LUFS,
                    normalize_lufs=BGM_NORMALIZE,
                    phase_check=BGM_PHASE_CHECK,
                )
                bgm_seg = project.add_audio_safe(
                    prepared_bgm_path,
                    start_time="0s",
                    duration=f"{speech_duration}s",
                    track_name="BGM",
                )
                if bgm_seg:
                    bgm_seg.volume = 0.45
                    if hasattr(bgm_seg, "add_fade"):
                        bgm_seg.add_fade("0.2s", "0.2s")
                    bgm_added = True
                    bgm_report["source_type"] = "user_bgm_dir"
                    bgm_report["voice_target_lufs"] = VOICE_TARGET_LUFS
                    _append_decision_log(
                        decision_log_path,
                        {
                            "event": "bgm_selected",
                            "source_type": "user_bgm_dir",
                            "source_path": chosen_bgm,
                            "prepared_path": prepared_bgm_path,
                            "target_lufs": BGM_TARGET_LUFS,
                            "voice_target_lufs": VOICE_TARGET_LUFS,
                            "pick_mode": BGM_PICK_MODE,
                            "phase_status": bgm_report["phase_report"]["status"],
                        },
                    )
                    if bgm_report.get("normalization_warning"):
                        _report_runtime_message(
                            "timeline_bgm_add",
                            f"BGM 响度归一化未生效，继续使用预处理音频: {bgm_report['normalization_warning']}",
                            level="WARN",
                            payload={"speech_video": speech_video, "bgm_path": chosen_bgm},
                        )
                    print(
                        "   [OK] 使用用户BGM: "
                        f"{os.path.basename(chosen_bgm)} | "
                        f"模式 {bgm_report['mode']} | "
                        f"响度 {bgm_report['target_lufs']} LUFS | "
                        f"相位 {bgm_report['phase_report']['status']}"
                    )
                if not bgm_added:
                    raise RuntimeError("未检测到用户背景音乐文件夹，请检查./my_bg_music/路径")
            except Exception as e:
                _append_runtime_issue(
                    "timeline_bgm_add",
                    "背景音乐轨道添加失败，已按非致命错误继续生成。",
                    exc=e,
                    payload={"speech_video": speech_video, "user_bgm_dir": USER_BGM_DIR},
                )
                print(f"   [WARN] BGM添加失败(非致命): {e}")

            # 7. 添加音效轨道（SFX）
            print("   添加音效轨道...")
            try:
                sfx_dir = os.path.join(os.path.dirname(speech_video), '..', '音效')
                sfx_dir = os.path.normpath(sfx_dir)
                local_sfx_files = []
                if os.path.isdir(sfx_dir):
                    for ext in ('*.mp3', '*.wav', '*.m4a', '*.aac'):
                        local_sfx_files.extend(glob.glob(os.path.join(sfx_dir, ext)))

                sfx_count = 0
                if local_sfx_files and sfx_list:
                    for sfx_item in sfx_list:
                        try:
                            sfx_file = random.choice(local_sfx_files)
                            sfx_seg = project.add_audio_safe(
                                sfx_file,
                                start_time=f"{sfx_item['time']}s",
                                duration="1.0s",
                                track_name="SFX"
                            )
                            if sfx_seg:
                                sfx_count += 1
                        except Exception as exc:
                            _append_runtime_issue(
                                "timeline_sfx_add",
                                "单条音效添加失败，已跳过当前音效继续生成。",
                                exc=exc,
                                payload={"sfx_file": sfx_file, "speech_video": speech_video},
                            )
                    if sfx_count > 0:
                        print(f"   [OK] 已根据 LLM 剧本精准添加 {sfx_count} 个音效")
                    else:
                        _report_runtime_message(
                            "timeline_sfx_add",
                            "本地音效添加失败",
                            level="SKIP",
                            payload={"speech_video": speech_video, "sfx_dir": sfx_dir},
                        )
                elif not sfx_list:
                    _report_runtime_message("timeline_sfx_add", "LLM 未规划音效", level="SKIP", payload={"speech_video": speech_video})
                else:
                    _report_runtime_message(
                        "timeline_sfx_add",
                        "音效未添加（未找到本地音效文件，请将音效放入 音效/ 目录）",
                        level="SKIP",
                        payload={"speech_video": speech_video, "sfx_dir": sfx_dir},
                    )
            except Exception as e:
                _report_runtime_message(
                    "timeline_sfx_add",
                    f"音效轨道添加失败: {e}",
                    level="SKIP",
                    payload={"speech_video": speech_video, "sfx_dir": sfx_dir},
                    exc=e,
                )

            # 8. 保存项目
            project.save()
            draft_path = validate_saved_draft(project)
            registry_report = reconcile_root_meta(
                draft_root=draft_root,
                restore_project_drafts=False,
                project_prefixes=("OTC推广_",),
                report_path=DRAFT_HEALTH_REPORT_PATH,
                lock_path=os.path.join(OUTPUT_DIR, ".root_meta_info.lock"),
            )
            restored_count = len(registry_report.get("restored_from_recycle", [])) + len(
                registry_report.get("restored_from_archive", [])
            )
            if restored_count or registry_report["invalid_drafts"]:
                print(
                    "   [监控] 草稿索引已修复: "
                    f"恢复 {restored_count} 个, "
                    f"发现无效目录 {len(registry_report['invalid_drafts'])} 个"
                )

            if is_portable_draft_root(draft_root):
                official_root = get_official_draft_root()
                if official_root:
                    try:
                        sync_report = sync_managed_drafts(
                            source_root=draft_root,
                            target_root=official_root,
                            project_prefixes=("OTC推广_",),
                            include_names=(unique_project_name,),
                            remove_stale_managed=False,
                        )
                        if sync_report.get("copied") or sync_report.get("replaced_invalid"):
                            print(f"   [同步] 草稿已同步至剪映系统目录: {os.path.join(official_root, unique_project_name)}")
                    except OSError:
                        print("   [!] 无法同步至系统剪映目录，请手动复制草稿文件夹。")
                else:
                    print("   [!] 未检测到系统剪映目录，草稿仅保存在便携目录中。")

            print(f"[成功] OTC推广视频草稿已创建: {project_name}")
            print(f"   草稿路径: {draft_path}")
            print(f"[成功] 最终视频时长: {speech_duration:.2f}秒 (与口播时长一致)")

        # 6. 输出时长分布统计及全片审查报告
        total_insert = sum(m['duration'] for m in matches)
        speech_visible = speech_duration - total_insert
        insert_ratio = (total_insert / speech_duration * 100) if speech_duration > 0 else 0
        track_hierarchy = [
            {"track_id": 1, "name": "01_Main_Video", "content": "主视频内容", "duration": speech_duration},
            {"track_id": 2, "name": "02_B_Roll", "content": "中插素材", "count": len(matches), "total_duration": total_insert},
        ]
        if is_review_version:
            track_hierarchy.extend(
                [
                    {"track_id": 3, "name": "05_Ad_Review", "content": "广审文件", "duration": speech_duration, "is_full_duration": True, "opacity": "100%", "position": "bottom_10%"},
                    {"track_id": 4, "name": "06_Top_Sticker", "content": "顶部贴图", "duration": speech_duration, "is_full_duration": True, "opacity": "100%", "position": "top_10%"},
                ]
            )
        
        # 写入 JSON 报告用于审核
        report_data = {
            "project_name": unique_project_name,
            "total_duration": speech_duration,
            "acceptance_check": {
                "duration_delta_sec": 0,
                "target_density_rule": f">={int(MIN_INSERTS_PER_WINDOW)}条/{int(DENSITY_WINDOW_SECONDS)}秒",
                "semantic_match_threshold": SEMANTIC_MATCH_THRESHOLD,
                "cascade_reuse_threshold": SEMANTIC_REUSE_THRESHOLD,
                "broll_ratio_target": TARGET_BROLL_COVERAGE_RATIO,
                "semantic_mix_target": BROLL_RATIO,
                "shot_recall_enabled": ENABLE_SHOT_RECALL,
                "shot_recall_use_vlm": SHOT_RECALL_USE_VLM,
                "shot_recall_force_refresh": SHOT_RECALL_FORCE_REFRESH,
                "shot_recall_vlm_model": SHOT_RECALL_VLM_MODEL.strip() or os.environ.get("LLM_MODEL", "").strip(),
                "strict_semantic_insertion": STRICT_SEMANTIC_INSERTION,
                "allow_blind_fallback": ALLOW_BLIND_FALLBACK,
                "generic_emergency_threshold": _resolve_emergency_threshold(),
                "decision_log_path": decision_log_path,
                "density_warning_path": os.path.join(OUTPUT_DIR, f"{os.path.splitext(os.path.basename(speech_video))[0]}_density_warning.txt"),
                "broll_ratio_report_path": os.path.join(OUTPUT_DIR, f"{os.path.splitext(os.path.basename(speech_video))[0]}_broll_ratio_report.json"),
            },
            "track_hierarchy": track_hierarchy,
            "export_requirements": {
                "format": "H.264 MP4",
                "resolution": "1080p (1920x1080)",
                "framerate": "25 fps",
                "bitrate": ">= 8 Mbps",
                "audio": "48 kHz / 16-bit Stereo",
                "delivery": ["干净版", "带时间码审查版", "对齐报告CSV"]
            },
            "statistics": {
                "host_face_duration": f"{speech_visible:.2f}s",
                "insert_duration": f"{total_insert:.2f}s",
                "insert_ratio": f"{insert_ratio:.1f}%",
                "symptom_insert_count": symptom_matches,
                "product_insert_count": product_matches,
            },
            "bgm_processing": bgm_report or {"status": "未添加"},
            "traceability": {
                "decision_log_path": decision_log_path,
                "semantic_understanding": {
                    "shot_recall_enabled": ENABLE_SHOT_RECALL,
                    "shot_recall_use_vlm": SHOT_RECALL_USE_VLM,
                    "shot_recall_force_refresh": SHOT_RECALL_FORCE_REFRESH,
                    "shot_recall_vlm_model": SHOT_RECALL_VLM_MODEL.strip() or os.environ.get("LLM_MODEL", "").strip(),
                    "shot_recall_top_n": SHOT_RECALL_TOP_N,
                    "shot_recall_max_videos": SHOT_RECALL_MAX_VIDEOS,
                    "strict_semantic_insertion": STRICT_SEMANTIC_INSERTION,
                    "allow_blind_fallback": ALLOW_BLIND_FALLBACK,
                    "generic_emergency_threshold": _resolve_emergency_threshold(),
                },
                "broll_insertions": [
                    {
                        "start_time": round(item["start_time"], 3),
                        "end_time": round(item["end_time"], 3),
                        "semantic_type": item.get("semantic_type"),
                        "trigger_reason": item.get("trigger_reason"),
                        "trigger_keyword": item.get("trigger_keyword"),
                        "material_name": item["material"]["filename"],
                        "material_path": item["material"]["path"],
                        "is_shot_material": bool(item.get("is_shot_material", False)),
                        "fallback_level": item.get("fallback_level", ""),
                        "match_quality": item.get("match_quality", "normal"),
                        "match_quality_label": item.get("match_quality_label", "标准语义命中"),
                        "semantic_score": round(float(item.get("semantic_score", 0.0)), 4),
                        "score_breakdown": dict(item.get("score_breakdown") or {}),
                        "shot_index": item.get("shot_index"),
                        "shot_start_seconds": item.get("shot_start_seconds"),
                        "shot_end_seconds": item.get("shot_end_seconds"),
                        "shot_caption": item.get("shot_caption", ""),
                        "shot_thumbnail_path": item.get("shot_thumbnail_path", ""),
                    }
                    for item in matches
                ],
            },
        }
        
        output_dir = OUTPUT_DIR
        os.makedirs(output_dir, exist_ok=True)
        report_path = os.path.join(output_dir, f"{unique_project_name}_审查报告.json")
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(report_data, f, ensure_ascii=False, indent=4)

        print(f"\n   [STAT] 时长分布统计与审查报告:")
        print(f"   {'='*45}")
        print(f"   视频总时长:     {speech_duration:.1f}秒")
        print(f"   中插素材总时长: {total_insert:.1f}秒 ({insert_ratio:.1f}%)")
        print(f"   口播可见时长:   {speech_visible:.1f}秒 ({100-insert_ratio:.1f}%)")
        print(f"   {'='*45}")
        target_insert_ratio_pct = TARGET_BROLL_COVERAGE_RATIO * 100
        if insert_ratio >= (target_insert_ratio_pct - 0.1):
            print(f"   [OK] 中插占比达标 ({insert_ratio:.1f}% >= {target_insert_ratio_pct:.0f}%)")
        else:
            print(f"   [!] 中插占比未达标 ({insert_ratio:.1f}% < {target_insert_ratio_pct:.0f}%)")
        print(f"\n   [DETAIL] 素材时间节点明细:")
        for i, m in enumerate(matches, 1):
            marker = "[T]转折点" if m.get('is_transition') else "[N]常规"
            print(f"   [{i:02d}] {m['start_time']:6.1f}s - {m['end_time']:6.1f}s | {m['duration']:.1f}s | {m['material_type']} | {marker} | {m['text'][:15]}")

        print(f"   [生成] 全片审查报告已保存: {report_path}")

        return True

    except Exception as e:
        print(f"[失败] 创建失败: {e}")
        # 如果创建失败，尝试清理已经创建但未完整保存的空草稿文件夹
        try:
            if 'project' in locals() and hasattr(project, 'root') and hasattr(project, 'name'):
                draft_path = os.path.join(project.root, project.name)
                if os.path.exists(draft_path):
                    import shutil
                    shutil.rmtree(draft_path)
                    print(f"   [清理] 已自动删除创建失败的空草稿: {project.name}")
                    reconcile_root_meta(
                        draft_root=get_draft_root(),
                        restore_project_drafts=False,
                        project_prefixes=("OTC推广_",),
                        report_path=DRAFT_HEALTH_REPORT_PATH,
                        lock_path=os.path.join(OUTPUT_DIR, ".root_meta_info.lock"),
                    )
        except Exception as cleanup_err:
            print(f"   [警告] 清理失败草稿时出错: {cleanup_err}")
        return False


def export_video(project_name: str, output_path: str, resolution: str = "1080", fps: str = "30") -> bool:
    """导出视频为MP4格式（需要剪映处于首页/编辑页面）"""
    try:
        print(f"\n正在导出视频: {output_path}")
        print("   提示: 请确保剪映已启动并停留在首页或编辑页面")

        from auto_exporter import auto_export
        code, result = auto_export(project_name, output_path, resolution=resolution, framerate=fps)

        if code == 0:
            print(f"[成功] 视频导出成功: {output_path}")
            return True
        else:
            print(f"[失败] 导出失败: {result}")
            print("   提示: 请重启剪映并保持在首页/编辑页面后重试")
            return False

    except Exception as e:
        print(f"[失败] 导出失败: {e}")
        print("   提示: 请确保剪映已启动，并在剪映中手动导出")
        return False


def main():
    """主工作流"""
    configure_current_process()
    import argparse
    parser = argparse.ArgumentParser(description='OTC药品推广视频自动化剪辑工作流')
    parser.add_argument('--sensitivity', '-s', choices=['medium', 'high'], default='medium',
                        help='素材插入灵敏度: medium=中密度, high=高密度')
    parser.add_argument('--video', '-v', type=str, default=None,
                        help='指定口播视频文件名（不指定则自动选择）')
    parser.add_argument('--preflight', action='store_true',
                        help='执行打包运行环境自检并输出 JSON 结果')
    args = parser.parse_args()

    if args.preflight:
        report = build_runtime_preflight_report()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if not report["fatal_errors"] else 1

    print("=" * 80)
    print("OTC药品推广视频智能剪辑工作流")
    print("=" * 80)
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"灵敏度设置: {args.sensitivity}\n")
    if _CONFIG_PARSE_WARNINGS:
        for warning in _CONFIG_PARSE_WARNINGS:
            print(f"   [WARN] 配置回退: {warning}")
            _append_runtime_issue("config_parse", warning)

    # 创建输出目录
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 1. 收集素材
    print("步骤1: 收集视频素材...")
    speech_videos = collect_video_files(SPEECH_DIR, log_skipped_audio=True, source_label="口播")
    product_videos = collect_video_files(PRODUCT_DIR)
    symptom_videos = collect_video_files(SYMPTOM_DIR)

    print(f"   - 口播视频: {len(speech_videos)} 个")
    print(f"   - 产品视频: {len(product_videos)} 个")
    print(f"   - 病症视频: {len(symptom_videos)} 个\n")

    if not speech_videos:
        print("错误: 没有找到口播视频！")
        return 1

    try:
        product_videos, symptom_videos, material_pool_report = validate_material_pools(
            product_videos=product_videos,
            symptom_videos=symptom_videos,
            sensitivity=args.sensitivity,
        )
        write_material_pool_report(material_pool_report, MATERIAL_POOL_REPORT_PATH)
        print("步骤1.1: 素材池校验通过")
        print(f"   - 产品独立素材: {material_pool_report['product_after']} 个")
        print(f"   - 病症独立素材: {material_pool_report['symptom_after']} 个")
        if material_pool_report["removed_products"] or material_pool_report["removed_symptoms"]:
            print(
                "   [监控] 已剔除重复/相似素材: "
                f"产品 {len(material_pool_report['removed_products'])} 个, "
                f"病症 {len(material_pool_report['removed_symptoms'])} 个"
            )
    except ValueError as exc:
        print(f"   错误: {exc}")
        return 1

    # 2. 选择口播视频（选择时长在3-5分钟的）
    print("步骤2: 选择合适的口播视频...")
    if args.video:
        is_valid_speech_video, validation_message = validate_speech_video_file(args.video)
        if not is_valid_speech_video:
            print(validation_message)
            return 0

        selected_video = _find_requested_video(args.video, speech_videos)
        if not selected_video:
            print(f"   错误: 未找到指定的视频文件: {args.video}")
            return 1
    else:
        suitable_videos = [v for v in speech_videos if 180 <= v['duration'] <= 300]
        if not suitable_videos:
            print("   未找到3-5分钟的视频，使用第一个视频")
            selected_video = speech_videos[0]
        else:
            selected_video = suitable_videos[0]
    
    print(f"   选择视频: {selected_video['filename']}")
    print(f"   视频时长: {selected_video['duration']:.1f}秒\n")

    # 3. 智能素材匹配（设置灵敏度）
    print("步骤3: 智能素材匹配 (时间驱动)...")
    sensitivity = args.sensitivity
    video_id = os.path.splitext(selected_video['filename'])[0]

    # 初始化 UsageTracker
    limits = {
        "ad_review": AD_FREQ_LIMIT,
        "sticker": STICKER_FREQ_LIMIT,
        "broll": BROLL_FREQ_LIMIT
    }
    tracker = UsageTracker(limits)

    matches, sfx_list, bgm_emotion = smart_material_matching(
        video_duration=selected_video['duration'],
        product_videos=product_videos,
        symptom_videos=symptom_videos,
        sensitivity=sensitivity,
        video_id=video_id,
        tracker=tracker,
        speech_video_path=selected_video['path'],
    )

    symptom_matches = sum(1 for m in matches if m['material_type'] == "病症困扰")
    product_matches = sum(1 for m in matches if m['material_type'] == "产品展示")

    print(f"   - 病症素材: {symptom_matches} 处")
    print(f"   - 产品素材: {product_matches} 处\n")

    # 4. 创建OTC推广视频
    print("步骤4: 创建OTC推广视频...")
    project_name = f"OTC推广_{os.path.splitext(selected_video['filename'])[0]}"
    success = create_otc_promo_video(
        project_name,
        selected_video['path'],
        matches,
        sfx_list=sfx_list,
        bgm_emotion=bgm_emotion,
        tracker=tracker,
        is_review_version=False
    )

    if success:
        print("\n" + "=" * 80)
        print("[成功] OTC药品推广视频创建成功！")
        print("=" * 80)
        print(f"项目名称: {project_name}")
        print(f"视频时长: {selected_video['duration']:.1f}秒 (与口播时长一致)")
        print(f"素材匹配: {len(matches)} 处")
        print(f"素材灵敏度: {sensitivity}")
        print("\n您可以在剪映中打开此草稿进行审核和微调")
        print("建议调整:")
        print("  1. 调整素材的转场效果")
        print("  2. 添加适当的背景音乐")
        print("  3. 确保符合OTC药品推广规范")
        print("  4. 如需调整素材密度，修改sensitivity参数")
        print("=" * 80)
    else:
        print("\n[失败] 视频创建失败，请检查错误信息")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
