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
import math
import random
import shutil
import subprocess
from typing import List, Dict, Optional, Tuple, Union
from datetime import datetime
from app_paths import (
    build_runtime_env,
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

# 环境初始化
os.environ.update(build_runtime_env(os.environ))
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
    build_material_semantic_library,
    rank_materials_by_semantics,
)
from bgm_pipeline import prepare_bgm_for_timeline
from timeline_utils import (
    layout_segments_on_tracks,
    sanitize_non_overlapping_segments,
    seconds_to_microseconds,
)

# 配置参数
VIDEO_DIR = os.environ.get("OTC_VIDEO_DIR", "H:\\体癣")
SPEECH_DIR = os.environ.get("OTC_SPEECH_DIR", os.path.join(VIDEO_DIR, "口播"))
PRODUCT_DIR = os.environ.get("OTC_PRODUCT_DIR", os.path.join(VIDEO_DIR, "产品"))
SYMPTOM_DIR = os.environ.get("OTC_SYMPTOM_DIR", os.path.join(VIDEO_DIR, "病症"))
AUDIO_DIR = os.environ.get("OTC_AUDIO_DIR", os.path.join(VIDEO_DIR, "音效"))
BGM_DIR = os.environ.get("OTC_BGM_DIR", os.path.join(VIDEO_DIR, "背景音乐"))
AD_REVIEW_DIR = os.environ.get("OTC_AD_REVIEW_DIR", os.path.join(VIDEO_DIR, "广审"))
STICKER_DIR = os.environ.get("OTC_STICKER_DIR", os.path.join(VIDEO_DIR, "贴图"))
OUTPUT_DIR = os.environ.get("OTC_OUTPUT_DIR", get_output_dir())
DRAFT_HEALTH_REPORT_PATH = os.path.join(OUTPUT_DIR, "draft_registry_health.json")

# 频率限制参数 (从 UI 获取，默认无限制为0)
AD_FREQ_LIMIT = int(os.environ.get("OTC_AD_FREQ", "1"))
STICKER_FREQ_LIMIT = int(os.environ.get("OTC_STICKER_FREQ", "0"))
BROLL_FREQ_LIMIT = int(os.environ.get("OTC_BROLL_FREQ", "1")) # 默认去重，同一中插只播放1次

# 成品配置参数
TEMPLATE_MODE = os.environ.get("OTC_TEMPLATE_MODE", "标准口播版")
RHYTHM_MODE = os.environ.get("OTC_RHYTHM_MODE", "常规呼吸感")
MIN_HOST_DURATION = float(os.environ.get("OTC_MIN_HOST_DURATION", "1.2"))
DENSITY_TEMPLATE = os.environ.get("OTC_DENSITY_TEMPLATE", "中节奏")
DENSITY_WINDOW_SECONDS = float(os.environ.get("OTC_DENSITY_WINDOW_SECONDS", "30"))
MIN_INSERTS_PER_WINDOW = int(os.environ.get("OTC_MIN_INSERTS_PER_WINDOW", "1"))
INSERT_MIN_DURATION = float(os.environ.get("OTC_INSERT_MIN_DURATION", "3.0"))
INSERT_MAX_DURATION = float(os.environ.get("OTC_INSERT_MAX_DURATION", "8.0"))
AUTO_FILL_DENSITY = os.environ.get("OTC_AUTO_FILL_DENSITY", "1") == "1"
BGM_CROSSFADE_MS = int(os.environ.get("OTC_BGM_CROSSFADE_MS", "200"))
BGM_TARGET_LUFS = int(os.environ.get("OTC_BGM_TARGET_LUFS", "-18"))
BGM_NORMALIZE = os.environ.get("OTC_BGM_NORMALIZE", "1") == "1"
BGM_PHASE_CHECK = os.environ.get("OTC_BGM_PHASE_CHECK", "1") == "1"
BROLL_RATIO = os.environ.get("OTC_BROLL_RATIO", "1:1").strip() or "1:1"
USER_BGM_DIR = os.environ.get("OTC_USER_BGM_DIR", runtime_path("my_bg_music"))
BGM_PICK_MODE = os.environ.get("OTC_BGM_PICK_MODE", "按文件名顺序").strip() or "按文件名顺序"
POOL_MIN_COUNT = int(os.environ.get("OTC_POOL_MIN_COUNT", "20"))
POOL_MIN_DURATION = float(os.environ.get("OTC_POOL_MIN_DURATION", "3.0"))
POOL_MAX_DURATION = float(os.environ.get("OTC_POOL_MAX_DURATION", "8.0"))
VOICE_TARGET_LUFS = int(os.environ.get("OTC_VOICE_TARGET_LUFS", "-9"))
DECISION_LOG_DIR = os.path.join(OUTPUT_DIR, "decision_logs")

MATERIAL_POOL_REPORT_PATH = os.path.join(OUTPUT_DIR, "material_pool_validation.json")
BROLL_USAGE_HISTORY_PATH = os.path.join(OUTPUT_DIR, "broll_usage_history.json")
SEMANTIC_MATCH_THRESHOLD = max(
    PRIMARY_SEMANTIC_THRESHOLD,
    float(os.environ.get("OTC_SEMANTIC_MATCH_THRESHOLD", str(PRIMARY_SEMANTIC_THRESHOLD))),
)
SEMANTIC_REUSE_THRESHOLD = max(
    CASCADE_REUSE_THRESHOLD,
    float(os.environ.get("OTC_SEMANTIC_REUSE_THRESHOLD", str(CASCADE_REUSE_THRESHOLD))),
)
TARGET_BROLL_COVERAGE_RATIO = max(0.0, min(0.95, float(os.environ.get("OTC_TARGET_BROLL_RATIO", str(WINDOW_TARGET_RATIO)))))
ALLOW_PREVIOUS_VIDEO_REUSE_WITHIN_24H = os.environ.get("OTC_ALLOW_PREVIOUS_VIDEO_REUSE_WITHIN_24H", "0") == "1"

class UsageTracker:
    """任务级素材使用追踪器，用于控制素材调用频率"""
    def __init__(self, limits: Dict[str, int]):
        self.limits = limits
        self.usage = {}
        self.history = [] # 记录使用历史，支持断点续播场景

    def can_use(self, item: Union[str, Dict], category: str) -> bool:
        limit = self.limits.get(category, 0)
        if limit == 0:
            return True
        uid = item.get('unique_id', item['path']) if isinstance(item, dict) else item
        return self.usage.get(uid, 0) < limit

    def record(self, item: Union[str, Dict]):
        uid = item.get('unique_id', item['path']) if isinstance(item, dict) else item
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
    except Exception:
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
    except Exception:
        pass
    return 0, 0


def _infer_material_emotion(tags: List[str], filename: str) -> str:
    tag_text = " ".join(tags + [filename])
    for level, keywords in EMOTION_STRENGTH_KEYWORDS.items():
        if any(keyword in tag_text for keyword in keywords):
            return level
    return "medium"


def _validate_tagged_material_pool(videos: List[Dict], pool_name: str, semantic_type: str):
    qualified = [
        item for item in videos
        if float(item.get("duration", 0.0)) >= POOL_MIN_DURATION
        and float(item.get("duration", 0.0)) <= POOL_MAX_DURATION
        and bool(item.get("is_vertical"))
        and item.get("tags")
    ]
    if len(qualified) < POOL_MIN_COUNT:
        print(
            f"   [WARN] {pool_name}当前满足标准的素材不足 {POOL_MIN_COUNT} 条，"
            f"仅检测到 {len(qualified)} 条，系统将继续生成但建议尽快补足。"
        )

    report = {
        "pool_name": pool_name,
        "semantic_type": semantic_type,
        "required_count": POOL_MIN_COUNT,
        "qualified_count": len(qualified),
        "qualified_examples": [os.path.basename(item["path"]) for item in qualified[:10]],
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
            print(f"   [SKIP] 跳过{source_label}音频文件: {os.path.basename(skipped_path)}")

    if not video_paths:
        return videos

    for filepath in video_paths:
        filename = os.path.basename(filepath)
        try:
            duration = _probe_media_duration(filepath)
            identity = build_media_identity(filepath, duration)

            # 解析标签：如 "[局部特写]_皮炎平.mp4"
            tags = []
            import re
            tag_match = re.findall(r'\[(.*?)\]', filename)
            if tag_match:
                for tm in tag_match:
                    tags.extend([t.strip() for t in tm.split(',') if t.strip()])

            width, height = _probe_media_dimensions(filepath)

            videos.append({
                'path': filepath,
                'filename': filename,
                'duration': duration,
                'type': os.path.basename(os.path.dirname(filepath)),
                'unique_id': identity['unique_id'],
                'content_hash': identity['content_hash'],
                'file_size': identity['file_size'],
                'tags': tags,
                'width': width,
                'height': height,
                'is_vertical': (height >= width) if width and height else False,
                'emotion_strength': _infer_material_emotion(tags, filename),
            })
        except Exception as e:
            print(f"Error reading video {filepath}: {e}")

    return videos


def _probe_media_duration(filepath: str) -> float:
    """用 ffprobe/ffmpeg 获取媒体时长，兼容视频与纯音频输入。"""
    try:
        result = run_hidden(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', filepath],
            capture_output=True, text=True, check=True, encoding='utf-8', errors='ignore'
        )
        duration = float(result.stdout.strip())
        if duration > 0:
            return duration
    except Exception:
        pass

    try:
        result = run_hidden(
            ['ffmpeg', '-i', filepath, '-f', 'null', '-'],
            capture_output=True, text=True, encoding='utf-8', errors='ignore'
        )
        duration_match = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.\d+)', result.stderr)
        if duration_match:
            h, m, s = duration_match.groups()
            return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        pass

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


def _select_material_from_ranked_pool(
    ranked_pool: List[Tuple[float, Dict]],
    *,
    threshold: float,
    tracker: UsageTracker,
    used_hashes: set,
    usage_history: Optional[BrollUsageHistory],
    allow_recent_reuse: bool,
) -> Tuple[Optional[Dict], float]:
    for score, material in ranked_pool:
        if score < threshold:
            continue
        content_hash = str(material.get("content_hash", ""))
        if content_hash and content_hash in used_hashes:
            continue
        if tracker and not tracker.can_use(material, "broll"):
            continue
        if (
            usage_history
            and content_hash
            and not allow_recent_reuse
            and usage_history.is_recently_used(content_hash)
        ):
            continue
        return material, score
    return None, 0.0


def _pick_semantic_material(
    candidate: Dict,
    product_videos: List[Dict],
    symptom_videos: List[Dict],
    emergency_videos: List[Dict],
    previous_video_videos: List[Dict],
    tracker: UsageTracker,
    used_hashes: set,
    usage_history: Optional[BrollUsageHistory],
) -> Tuple[Optional[Dict], Optional[str], float, str]:
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

    material, score = _select_material_from_ranked_pool(
        ranked_primary,
        threshold=SEMANTIC_MATCH_THRESHOLD,
        tracker=tracker,
        used_hashes=used_hashes,
        usage_history=usage_history,
        allow_recent_reuse=False,
    )
    if material:
        return material, material_type, score, "semantic_primary"

    material, score = _select_material_from_ranked_pool(
        ranked_previous,
        threshold=SEMANTIC_REUSE_THRESHOLD,
        tracker=tracker,
        used_hashes=used_hashes,
        usage_history=usage_history,
        allow_recent_reuse=ALLOW_PREVIOUS_VIDEO_REUSE_WITHIN_24H,
    )
    if material:
        return material, material_type, score, "previous_video_reuse"

    material, score = _select_material_from_ranked_pool(
        ranked_emergency,
        threshold=0.40,
        tracker=tracker,
        used_hashes=used_hashes,
        usage_history=usage_history,
        allow_recent_reuse=False,
    )
    if material:
        return material, material_type, score, "emergency_generic"

    # === 最终兜底：忽略语义评分，从主素材池盲选，确保中插占比达标 ===
    if primary_pool:
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
            return material, material_type, 0.0, "blind_fallback"

    return None, None, 0.0, "rejected"


def _materialize_broll_candidates(
    candidates: List[Dict],
    product_videos: List[Dict],
    symptom_videos: List[Dict],
    tracker: UsageTracker,
    video_id: str = "",
    decision_log_path: Optional[str] = None,
) -> List[Dict]:
    matches: List[Dict] = []
    usage_history = BrollUsageHistory(BROLL_USAGE_HISTORY_PATH)
    current_used_hashes = set()

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

    for candidate in candidates:
        material, material_type, semantic_score, fallback_level = _pick_semantic_material(
            candidate=candidate,
            product_videos=product_library,
            symptom_videos=symptom_library,
            emergency_videos=emergency_pool,
            previous_video_videos=previous_video_pool,
            tracker=tracker,
            used_hashes=current_used_hashes,
            usage_history=usage_history,
        )
        if not material:
            if decision_log_path:
                _append_decision_log(
                    decision_log_path,
                    {
                        "event": "broll_material_rejected",
                        "semantic_type": candidate.get("semantic_type"),
                        "text": candidate.get("text", ""),
                        "reason": "no_material_meets_semantic_threshold",
                        "threshold_primary": SEMANTIC_MATCH_THRESHOLD,
                        "threshold_cascade": SEMANTIC_REUSE_THRESHOLD,
                    },
                )
            continue

        if tracker:
            tracker.record(material)
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
            "trigger_reason": candidate.get("trigger_reason", "semantic"),
            "trigger_keyword": candidate.get("trigger_keyword"),
            "emotion_strength": candidate.get("emotion_strength", "medium"),
            "semantic_score": semantic_score,
            "fallback_level": fallback_level,
            "content_hash": material.get("content_hash", ""),
        }
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
                    "semantic_score": semantic_score,
                    "fallback_level": fallback_level,
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
) -> List[Dict]:
    """纯时间驱动：按节奏模板将视频等分为时间窗口，交替分配语义类型生成中插候选"""
    strategy = _get_broll_strategy_config(sensitivity)
    if video_duration <= 0:
        return []

    candidates: List[Dict] = []
    total_windows = max(1, int(math.ceil(video_duration / (strategy.get("long_block_threshold", 5.0)))))
    preferred_types = _preferred_semantic_sequence(total_windows * 2, BROLL_RATIO)

    for idx in range(total_windows):
        window_start = idx * (video_duration / total_windows)
        window_end = min(video_duration, (idx + 1) * (video_duration / total_windows))
        semantic_type = preferred_types[idx % len(preferred_types)]
        desired_duration = min(insert_max_duration, max(insert_min_duration, (window_end - window_start) * 0.65))
        center = (window_start + window_end) / 2
        start = max(0.0, center - desired_duration / 2)
        end = min(video_duration, start + desired_duration)
        if end - start < insert_min_duration:
            continue
        candidates.append({
            "start_time": round(start, 3),
            "end_time": round(end, 3),
            "duration": round(end - start, 3),
            "semantic_type": semantic_type,
            "text": f"{semantic_type}_time_based",
            "is_transition": False,
            "is_density_fill": True,
            "trigger_reason": "time_based",
        })

    return candidates


def smart_material_matching(
    video_duration: float,
    product_videos: List[Dict],
    symptom_videos: List[Dict],
    sensitivity: str = 'medium',
    video_id: str = "default_video",
    tracker: UsageTracker = None
) -> Tuple[List[Dict], List[Dict], str]:
    """纯时间驱动 + 密度自动补齐至 65% 的智能素材匹配"""
    print(f"正在进行智能素材匹配 (时间驱动版)...")

    if video_duration <= 0:
        print("   ⚠ 无效的视频时长，无法匹配素材")
        return [], [], "neutral"

    import csv

    llm_api_key = os.environ.get("LLM_API_KEY", "").strip()
    llm_base_url = os.environ.get("LLM_BASE_URL", "").strip()
    llm_model = os.environ.get("LLM_MODEL", "deepseek-v3.2").strip()

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

    # LLM 剧本生成（基于时长，无需字幕）
    if llm_api_key:
        try:
            import llm_clip_matcher
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
    )

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
        print(f"   [WARN] 发现 {len(pending_windows)} 个时间窗口中插密度不足")

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
        print(f"   [WARN] {density_warning}")

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
                    'semantic_score': round(float(m.get('semantic_score', 0.0)), 4),
                    'fallback_level': m.get('fallback_level', 'semantic_primary'),
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
        print(
            f"   [WARN] 中插总占比 {ratio_report['total_ratio']:.1%} 低于目标 "
            f"{TARGET_BROLL_COVERAGE_RATIO:.0%}，已输出占比监控报告。"
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
            print(
                f"   [WARN] 检测到当前剪映版本为 {detected_version}，"
                "非 5.9 兼容版本。将继续生成草稿，但无法保证可直接在剪映中打开。"
            )
        if not detected_version:
            print("   [WARN] 未能自动识别剪映版本，将继续尝试生成草稿。")
        else:
            print(f"   检测到剪映版本: {detected_version}")
        
        # 获取口播视频时长
        speech_duration = 0
        import subprocess
        try:
            result = run_hidden(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', speech_video],
                capture_output=True, text=True, check=True, encoding='utf-8', errors='ignore'
            )
            speech_duration = float(result.stdout.strip())
        except Exception:
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
            except Exception:
                print("   [WARN] ⚠ 无法通过 ffprobe 和 ffmpeg 获取视频时长，回退为 60 秒默认值。")
                print("   [WARN] 中插密度、时间线排布和覆盖率计算可能不准确，请检查视频文件是否损坏。")
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
                    except Exception:
                        pass

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
                    import random as _random
                    chosen_ad = _random.choice(local_ad_files)
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
                    print("   [SKIP] 广审素材未添加（未找到有效文件或已达到使用上限）")
            except Exception as e:
                print(f"   [SKIP] 广审轨道添加失败: {e}")

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
                    import random as _random
                    chosen_sticker = _random.choice(local_sticker_files)
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
                    print("   [SKIP] 贴图素材未添加（未找到有效文件或已达到使用上限）")
            except Exception as e:
                print(f"   [SKIP] 贴图添加失败: {e}")

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
                    import random as _random
                    for sfx_item in sfx_list:
                        try:
                            sfx_file = _random.choice(local_sfx_files)
                            sfx_seg = project.add_audio_safe(
                                sfx_file,
                                start_time=f"{sfx_item['time']}s",
                                duration="1.0s",
                                track_name="SFX"
                            )
                            if sfx_seg:
                                sfx_count += 1
                        except Exception:
                            pass
                    if sfx_count > 0:
                        print(f"   [OK] 已根据 LLM 剧本精准添加 {sfx_count} 个音效")
                    else:
                        print("   [SKIP] 本地音效添加失败")
                elif not sfx_list:
                    print("   [SKIP] LLM 未规划音效")
                else:
                    print("   [SKIP] 音效未添加（未找到本地音效文件，请将音效放入 音效/ 目录）")
            except Exception as e:
                print(f"   [SKIP] 音效轨道添加失败: {e}")

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

            print(f"[成功] OTC推广视频草稿已创建: {project_name}")
            print(f"   草稿路径: {draft_path}")
            print(f"[成功] 最终视频时长: {speech_duration:.2f}秒 (与口播时长一致)")

        # 6. 输出时长分布统计及全片审查报告
        total_insert = sum(m['duration'] for m in matches)
        speech_visible = speech_duration - total_insert
        insert_ratio = (total_insert / speech_duration * 100) if speech_duration > 0 else 0
        
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
                "decision_log_path": decision_log_path,
                "density_warning_path": os.path.join(OUTPUT_DIR, f"{os.path.splitext(os.path.basename(speech_video))[0]}_density_warning.txt"),
                "broll_ratio_report_path": os.path.join(OUTPUT_DIR, f"{os.path.splitext(os.path.basename(speech_video))[0]}_broll_ratio_report.json"),
            },
            "track_hierarchy": [
                {"track_id": 1, "name": "01_Main_Video", "content": "主视频内容", "duration": speech_duration},
                {"track_id": 2, "name": "02_B_Roll", "content": "中插素材", "count": len(matches), "total_duration": total_insert},
                {"track_id": 3, "name": "05_Ad_Review", "content": "广审文件", "duration": speech_duration, "is_full_duration": True, "opacity": "100%", "position": "bottom_10%"},
                {"track_id": 4, "name": "06_Top_Sticker", "content": "顶部贴图", "duration": speech_duration, "is_full_duration": True, "opacity": "100%", "position": "top_10%"},
            ],
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
                "broll_insertions": [
                    {
                        "start_time": round(item["start_time"], 3),
                        "end_time": round(item["end_time"], 3),
                        "semantic_type": item.get("semantic_type"),
                        "trigger_reason": item.get("trigger_reason"),
                        "trigger_keyword": item.get("trigger_keyword"),
                        "material_name": item["material"]["filename"],
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
        tracker=tracker
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
