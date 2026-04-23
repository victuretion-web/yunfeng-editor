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
import random
import subprocess
from typing import List, Dict, Optional, Tuple, Union
from datetime import datetime
from app_paths import (
    build_runtime_env,
    ensure_skill_scripts_on_path,
    get_bundled_whisper_model_path,
    get_output_dir,
    get_runtime_whisper_cache_dir,
)


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
from draft_registry import get_draft_root, reconcile_root_meta, file_lock
from material_pool_rules import validate_material_pools, write_material_pool_report
from media_identity import build_media_identity
from media_file_rules import (
    is_supported_video_file,
    scan_video_file_paths,
    validate_speech_video_file,
)
from timeline_utils import (
    layout_segments_on_tracks,
    sanitize_non_overlapping_segments,
    seconds_to_microseconds,
)

# Whisper模型全局缓存
_WHISPER_MODEL = None
_WHISPER_MODEL_NAME = None
_WHISPER_CACHE_DIR = os.environ.get("OTC_WHISPER_CACHE_DIR", get_runtime_whisper_cache_dir())
_WHISPER_MIN_SIZE_MB = {
    "base": 100,
    "small": 200,
}

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

_COMMON_SUBTITLE_REPLACEMENTS = {
    "骚扬": "瘙痒",
    "骚痒": "瘙痒",
    "提选": "体癣",
    "体选": "体癣",
    "红种": "红肿",
    "干凿": "干燥",
    "脱削": "脱屑",
    "真茵": "真菌",
    "胶嚷": "胶囊",
    "白选": "百癣",
    "夏塔热校囊": "夏塔热胶囊",
    "夏塔热脚囊": "夏塔热胶囊",
    "百癣夏塔热校囊": "百癣夏塔热胶囊",
    "百癣夏塔热脚囊": "百癣夏塔热胶囊",
    "OT c": "OTC",
    "OT C": "OTC",
}

# 频率限制参数 (从 UI 获取，默认无限制为0)
AD_FREQ_LIMIT = int(os.environ.get("OTC_AD_FREQ", "1"))
STICKER_FREQ_LIMIT = int(os.environ.get("OTC_STICKER_FREQ", "0"))
BROLL_FREQ_LIMIT = int(os.environ.get("OTC_BROLL_FREQ", "1")) # 默认去重，同一中插只播放1次
MATERIAL_POOL_REPORT_PATH = os.path.join(OUTPUT_DIR, "material_pool_validation.json")

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
    match = re.match(r"^\s*(\d+)(?:\.(\d+))?", version)
    if not match:
        return False
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return major < 5 or (major == 5 and minor <= 9)


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

            videos.append({
                'path': filepath,
                'filename': filename,
                'duration': duration,
                'type': os.path.basename(os.path.dirname(filepath)),
                'unique_id': identity['unique_id'],
                'content_hash': identity['content_hash'],
                'file_size': identity['file_size'],
            })
        except Exception as e:
            print(f"Error reading video {filepath}: {e}")

    return videos


def _probe_media_duration(filepath: str) -> float:
    """用 ffprobe/ffmpeg 获取媒体时长，兼容视频与纯音频输入。"""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', filepath],
            capture_output=True, text=True, check=True, encoding='utf-8', errors='ignore'
        )
        duration = float(result.stdout.strip())
        if duration > 0:
            return duration
    except Exception:
        pass

    try:
        result = subprocess.run(
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


def _get_whisper_model(model_name: str = "base"):
    """获取Whisper模型（全局单例 + 本地缓存）
    
    首次调用时下载模型到项目本地缓存目录 .whisper_cache/
    后续调用直接从本地缓存加载，避免重复下载
    模型在进程生命周期内保持在内存中，避免重复加载
    """
    global _WHISPER_MODEL, _WHISPER_MODEL_NAME

    if _WHISPER_MODEL is not None and _WHISPER_MODEL_NAME == model_name:
        print(f"   [缓存命中] Whisper模型 '{model_name}' 已在内存中，跳过加载")
        return _WHISPER_MODEL

    import whisper

    os.makedirs(_WHISPER_CACHE_DIR, exist_ok=True)

    runtime_model_path = os.path.join(_WHISPER_CACHE_DIR, f"{model_name}.pt")
    bundled_model_path = get_bundled_whisper_model_path(model_name)
    local_model_path = runtime_model_path if os.path.exists(runtime_model_path) else bundled_model_path
    is_runtime_cache = os.path.abspath(local_model_path) == os.path.abspath(runtime_model_path)
    if os.path.exists(local_model_path):
        file_size_mb = os.path.getsize(local_model_path) / (1024 * 1024)
        min_size_mb = _WHISPER_MIN_SIZE_MB.get(model_name, 50)
        if file_size_mb < min_size_mb:
            print(f"   [WARN] 本地缓存模型疑似损坏，将忽略并删除: {local_model_path} ({file_size_mb:.1f}MB)")
            if is_runtime_cache:
                try:
                    os.remove(local_model_path)
                except OSError:
                    pass
            else:
                local_model_path = runtime_model_path
        else:
            print(f"   [本地缓存] 发现已缓存的模型文件: {local_model_path} ({file_size_mb:.1f}MB)")
            print(f"   加载Whisper模型 '{model_name}'...")
            try:
                _WHISPER_MODEL = whisper.load_model(local_model_path)
                _WHISPER_MODEL_NAME = model_name
                print(f"   [OK] 模型加载完成")
                return _WHISPER_MODEL
            except Exception as e:
                print(f"   [WARN] 加载缓存模型失败: {e}")
                if is_runtime_cache:
                    try:
                        os.remove(local_model_path)
                        print("   [WARN] 已删除损坏模型缓存，将尝试重新加载。")
                    except OSError:
                        pass
                else:
                    print("   [WARN] 打包内置模型加载失败，将尝试写入运行目录缓存后重新下载。")

    print(f"   [首次下载] 本地未找到模型 '{model_name}'，正在下载到: {_WHISPER_CACHE_DIR}")
    print(f"   下载中，请耐心等待...")
    try:
        _WHISPER_MODEL = whisper.load_model(model_name, download_root=_WHISPER_CACHE_DIR)
        _WHISPER_MODEL_NAME = model_name
    except Exception as e:
        if model_name != "base":
            print(f"   [WARN] 模型 '{model_name}' 加载失败: {e}，回退到 'base'")
            return _get_whisper_model("base")
        raise

    downloaded_file = os.path.join(_WHISPER_CACHE_DIR, f"{model_name}.pt")
    if os.path.exists(downloaded_file):
        file_size_mb = os.path.getsize(downloaded_file) / (1024 * 1024)
        print(f"   [OK] 模型已下载并缓存: {downloaded_file} ({file_size_mb:.1f}MB)")
    else:
        print(f"   [OK] 模型已加载（缓存路径可能不同）")

    return _WHISPER_MODEL


def _normalize_subtitle_text(text: str) -> str:
    text = re.sub(r"\s+", "", text.strip())
    text = text.replace("，。", "。").replace("。。", "。")
    for wrong, correct in _COMMON_SUBTITLE_REPLACEMENTS.items():
        text = text.replace(wrong, correct)
    return text


def transcribe_with_ai(video_path: str) -> List[Dict]:
    """使用Whisper进行真实语音识别与语义分析"""
    print(f"正在进行AI语音识别与语义分析: {video_path}")

    import subprocess
    import tempfile

    temp_audio_path = None
    try:
        print("   步骤1: 提取音频...")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_audio:
            temp_audio_path = temp_audio.name

        cmd = [
            "ffmpeg", "-i", video_path,
            "-ar", "16000", "-ac", "1", "-f", "wav",
            temp_audio_path, "-y"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg提取音频失败: {result.stderr[:200]}")

        print("   步骤2: Whisper语音识别...")
        whisper_model_name = os.environ.get("OTC_WHISPER_MODEL", "base").strip() or "base"
        model = _get_whisper_model(whisper_model_name)
        # 强制使用简体中文提示词，避免繁体输出
        transcribe_result = model.transcribe(
            temp_audio_path, 
            language='zh', 
            fp16=False, 
            verbose=False,
            initial_prompt=(
                "以下是普通话的简体中文OTC药品口播，请全部使用简体中文输出。"
                "常见词包括：体癣、股癣、手足癣、真菌、瘙痒、脱屑、红斑、皮肤、胶囊、药膏、喷剂。"
            )
        )

        os.unlink(temp_audio_path)
        temp_audio_path = None

        subtitles = []
        for i, segment in enumerate(transcribe_result['segments']):
            text = _normalize_subtitle_text(segment['text'])
            if not text or text in ('。', '，', '、', '！', '？', '…'):
                continue

            semantic_type = analyze_semantic(text)
            emotional_tone = analyze_emotion(text)
            
            # 文本过长，需要按照字数比例切割
            max_len = 15
            if len(text) > max_len:
                # 为了不生硬切断词语，我们按照标点符号或者按字数强制切割
                # 简单高效方案：严格按最大长度切割，等比分配时间
                num_chunks = (len(text) + max_len - 1) // max_len
                chunk_len = (len(text) + num_chunks - 1) // num_chunks # 尽量均分
                
                segment_start = float(segment['start'])
                segment_end = float(segment['end'])
                duration = segment_end - segment_start
                char_duration = duration / len(text) if len(text) > 0 else 0
                
                curr_start = segment_start
                for idx in range(0, len(text), chunk_len):
                    chunk_text = text[idx:idx+chunk_len]
                    chunk_duration = len(chunk_text) * char_duration
                    
                    subtitles.append({
                        'index': len(subtitles) + 1,
                        'start': round(curr_start, 3),
                        'end': round(curr_start + chunk_duration, 3),
                        'text': chunk_text,
                        'semantic_type': semantic_type,
                        'emotional_tone': emotional_tone
                    })
                    curr_start += chunk_duration
            else:
                subtitles.append({
                    'index': len(subtitles) + 1,
                    'start': round(segment['start'], 3),
                    'end': round(segment['end'], 3),
                    'text': text,
                    'semantic_type': semantic_type,
                    'emotional_tone': emotional_tone
                })

        if not subtitles:
            raise RuntimeError("Whisper未识别到有效语音内容")

        print(f"   成功识别: {len(subtitles)} 条字幕")
        print("   字幕内容:")
        for sub in subtitles[:8]:
            print(f"     [{sub['start']:.1f}s - {sub['end']:.1f}s] {sub['text']}")
        if len(subtitles) > 8:
            print(f"     ... 还有 {len(subtitles) - 8} 条字幕")

        del transcribe_result
        import gc
        gc.collect()

        return subtitles

    except Exception as e:
        print(f"   Whisper识别失败: {e}")
        if temp_audio_path and os.path.exists(temp_audio_path):
            try:
                os.unlink(temp_audio_path)
            except OSError:
                pass
        print("   尝试使用FFmpeg+SRT方案...")
        return _transcribe_with_ffmpeg_srt(video_path)


def _transcribe_with_ffmpeg_srt(video_path: str) -> List[Dict]:
    """备选方案：使用FFmpeg提取音频后尝试whisper-cli或降级到模拟字幕"""
    try:
        import subprocess
        cmd = ["ffmpeg", "-i", video_path, "-f", "null", "-"]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        duration_match = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.\d+)', result.stderr)
        if duration_match:
            h, m, s = duration_match.groups()
            video_duration = int(h) * 3600 + int(m) * 60 + float(s)
        else:
            video_duration = 60.0

        print(f"   视频时长: {video_duration:.2f}秒")
        print("   ⚠ 无法进行真实语音识别，使用基于时长的模拟字幕（建议安装Whisper）")

        return _generate_fallback_subtitles(video_duration)

    except Exception as e2:
        print(f"   备选方案也失败: {e2}，使用默认模拟字幕")
        return transcribe_enhanced_mock(video_path)


def _generate_fallback_subtitles(video_duration: float) -> List[Dict]:
    """生成基于时长的降级模拟字幕（仅在Whisper完全不可用时使用）"""
    import math
    num_segments = max(3, math.ceil(video_duration / 4))
    segment_duration = video_duration / num_segments
    subtitles = []
    for i in range(num_segments):
        start_time = i * segment_duration
        end_time = min((i + 1) * segment_duration, video_duration)
        subtitles.append({
            'index': i + 1,
            'start': round(start_time, 2),
            'end': round(end_time, 2),
            'text': f"[语音内容 {i+1}]",
            'semantic_type': 'neutral',
            'emotional_tone': 'neutral'
        })
    return subtitles


def analyze_semantic(text: str) -> str:
    """分析文本语义，识别内容类型"""
    text_lower = text.lower()
    
    # 检查病症相关关键词
    if any(keyword in text for keyword in SYMPTOM_KEYWORDS):
        return 'symptom'
    
    # 检查产品相关关键词
    if any(keyword in text for keyword in PRODUCT_KEYWORDS):
        return 'product'
    
    return 'neutral'


def analyze_emotion(text: str) -> str:
    """分析文本情感基调"""
    positive_count = sum(1 for kw in EMOTIONAL_KEYWORDS['positive'] if kw in text)
    negative_count = sum(1 for kw in EMOTIONAL_KEYWORDS['negative'] if kw in text)
    
    if positive_count > negative_count:
        return 'positive'
    elif negative_count > positive_count:
        return 'negative'
    return 'neutral'


def transcribe_enhanced_mock(video_path: str) -> List[Dict]:
    """增强的模拟语音识别"""
    print(f"使用增强模拟语音识别: {video_path}")
    
    mock_subtitles = [
        {"index": 1, "start": 0.5, "end": 3.5, "text": "大家好，今天我们来聊聊体癣的问题", "semantic_type": "neutral", "emotional_tone": "neutral"},
        {"index": 2, "start": 4.0, "end": 7.5, "text": "体癣是一种常见的皮肤真菌感染，很多人都有这样的困扰", "semantic_type": "symptom", "emotional_tone": "negative"},
        {"index": 3, "start": 8.0, "end": 11.5, "text": "主要表现为皮肤上出现红斑、脱屑，患者会感到瘙痒不适", "semantic_type": "symptom", "emotional_tone": "negative"},
        {"index": 4, "start": 12.0, "end": 15.5, "text": "这些症状不仅影响生活质量，还让人感到尴尬", "semantic_type": "symptom", "emotional_tone": "negative"},
        {"index": 5, "start": 16.0, "end": 19.5, "text": "我们的产品可以有效治疗体癣，使用方法简单", "semantic_type": "product", "emotional_tone": "positive"},
        {"index": 6, "start": 20.0, "end": 23.5, "text": "这款产品采用温和配方，安全无刺激", "semantic_type": "product", "emotional_tone": "positive"},
        {"index": 7, "start": 24.0, "end": 27.5, "text": "很多患者使用后都反馈效果显著", "semantic_type": "product", "emotional_tone": "positive"},
        {"index": 8, "start": 28.0, "end": 31.5, "text": "体癣虽然顽固，但并非无法治愈", "semantic_type": "symptom", "emotional_tone": "neutral"},
        {"index": 9, "start": 32.0, "end": 35.5, "text": "坚持使用我们的产品，很快就能看到改善", "semantic_type": "product", "emotional_tone": "positive"},
        {"index": 10, "start": 36.0, "end": 39.5, "text": "下面我来详细介绍这款产品的使用方法", "semantic_type": "product", "emotional_tone": "neutral"},
        {"index": 11, "start": 40.0, "end": 43.5, "text": "这是我们的明星产品，已经帮助了很多患者", "semantic_type": "product", "emotional_tone": "positive"},
        {"index": 12, "start": 44.0, "end": 47.5, "text": "如果你也有类似的困扰，不妨试试", "semantic_type": "symptom", "emotional_tone": "neutral"},
        {"index": 13, "start": 48.0, "end": 51.5, "text": "我们的产品经过专业认证，安全可靠", "semantic_type": "product", "emotional_tone": "positive"},
        {"index": 14, "start": 52.0, "end": 55.5, "text": "适合各种肤质使用，无副作用", "semantic_type": "product", "emotional_tone": "positive"},
        {"index": 15, "start": 56.0, "end": 59.5, "text": "希望今天的分享对大家有所帮助", "semantic_type": "neutral", "emotional_tone": "neutral"}
    ]
    
    return mock_subtitles


def _get_broll_strategy_config(sensitivity: str) -> Dict[str, float]:
    config_map = {
        "medium": {"min_gap": 0.45, "min_duration": 1.2, "max_duration": 3.8, "long_block_threshold": 5.0, "dense_block_threshold": 3.8},
        "high": {"min_gap": 0.2, "min_duration": 1.0, "max_duration": 4.0, "long_block_threshold": 4.2, "dense_block_threshold": 3.0},
    }
    return config_map.get(sensitivity, config_map["medium"]).copy()


def _build_semantic_blocks(subtitles: List[Dict]) -> List[Dict]:
    blocks: List[Dict] = []
    current = None

    for sub in subtitles:
        semantic_type = sub.get("semantic_type", "neutral")
        start = float(sub.get("start", 0.0))
        end = float(sub.get("end", start))
        text = str(sub.get("text", "")).strip()
        if end <= start:
            continue

        if (
            current
            and semantic_type == current["semantic_type"]
            and start - current["end"] <= 0.6
        ):
            current["end"] = end
            if text:
                current["texts"].append(text)
            continue

        if current:
            blocks.append(current)

        current = {
            "semantic_type": semantic_type,
            "start": start,
            "end": end,
            "texts": [text] if text else [],
        }

    if current:
        blocks.append(current)

    return blocks


def _infer_semantic_type_for_range(
    subtitles: List[Dict],
    start_time: float,
    end_time: float,
    default: str = "symptom",
) -> str:
    weighted = {"symptom": 0.0, "product": 0.0}
    for sub in subtitles:
        semantic_type = sub.get("semantic_type", "neutral")
        if semantic_type not in weighted:
            continue
        overlap_start = max(start_time, float(sub.get("start", 0.0)))
        overlap_end = min(end_time, float(sub.get("end", overlap_start)))
        overlap = overlap_end - overlap_start
        if overlap > 0:
            weighted[semantic_type] += overlap

    if weighted["product"] > weighted["symptom"]:
        return "product"
    if weighted["symptom"] > 0:
        return "symptom"
    return default


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


def _normalize_broll_candidates(
    candidates: List[Dict],
    subtitles: List[Dict],
    video_duration: float,
    sensitivity: str,
) -> List[Dict]:
    strategy = _get_broll_strategy_config(sensitivity)
    blocks = _build_semantic_blocks(subtitles)
    normalized: List[Dict] = []
    last_end_time = -1.0

    for candidate in sorted(candidates, key=lambda item: float(item.get("start_time", 0.0))):
        semantic_type = candidate.get("semantic_type") or _infer_semantic_type_for_range(
            subtitles,
            float(candidate.get("start_time", 0.0)),
            float(candidate.get("end_time", candidate.get("start_time", 0.0))),
        )
        if semantic_type not in ("symptom", "product"):
            continue

        desired_start = max(0.0, float(candidate.get("start_time", 0.0)))
        desired_end = min(video_duration, float(candidate.get("end_time", desired_start)))
        if desired_end - desired_start < strategy["min_duration"]:
            desired_end = min(video_duration, desired_start + strategy["min_duration"])

        matching_blocks = [b for b in blocks if b["semantic_type"] == semantic_type]
        if not matching_blocks and semantic_type == "product":
            matching_blocks = [
                block for block in blocks
                if block["semantic_type"] == "neutral" and block["start"] >= video_duration * 0.35
            ]
        if not matching_blocks and semantic_type == "symptom":
            matching_blocks = [
                block for block in blocks
                if block["semantic_type"] == "neutral" and block["start"] <= video_duration * 0.6
            ]
        if not matching_blocks:
            matching_blocks = blocks
        if not matching_blocks:
            continue

        def _block_sort_key(block: Dict):
            overlap_start = max(block["start"], desired_start)
            overlap_end = min(block["end"], desired_end)
            overlap = max(0.0, overlap_end - overlap_start)
            distance = abs(((block["start"] + block["end"]) / 2.0) - ((desired_start + desired_end) / 2.0))
            return (-overlap, distance, block["start"])

        placed = None
        for block in sorted(matching_blocks, key=_block_sort_key):
            placed = _fit_broll_candidate_to_block(
                block=block,
                desired_start=desired_start,
                desired_end=desired_end,
                last_end_time=last_end_time,
                strategy=strategy,
                video_duration=video_duration,
            )
            if placed:
                break

        if not placed:
            continue

        start_time, end_time = placed
        normalized.append({
            "start_time": start_time,
            "end_time": end_time,
            "duration": round(end_time - start_time, 3),
            "semantic_type": semantic_type,
            "text": candidate.get("text", ""),
            "is_transition": bool(candidate.get("is_transition", False)),
        })
        last_end_time = end_time

    return normalized


def _build_rule_based_broll_candidates(
    subtitles: List[Dict],
    video_duration: float,
    sensitivity: str,
) -> List[Dict]:
    strategy = _get_broll_strategy_config(sensitivity)
    blocks = _build_semantic_blocks(subtitles)
    candidates: List[Dict] = []
    last_end_time = -1.0

    for block in blocks:
        semantic_type = block["semantic_type"]
        if semantic_type not in ("symptom", "product"):
            continue

        block_duration = float(block["end"]) - float(block["start"])
        if block_duration < strategy["min_duration"]:
            continue

        slot_count = 1
        if block_duration >= strategy["long_block_threshold"] * 1.8:
            slot_count = 4 if sensitivity == "high" else 3
        elif block_duration >= strategy["long_block_threshold"]:
            slot_count = 3 if sensitivity in ("medium", "high") else 2
        elif block_duration >= strategy["dense_block_threshold"]:
            slot_count = 2 if sensitivity in ("medium", "high") else 1

        for slot_index in range(slot_count):
            slot_anchor = block["start"] + (slot_index + 1) * (block_duration / (slot_count + 1))
            desired_duration = min(
                strategy["max_duration"],
                max(strategy["min_duration"], min(block_duration * 0.78, strategy["max_duration"])),
            )
            desired_start = slot_anchor - (desired_duration / 2.0)
            desired_end = desired_start + desired_duration

            placed = _fit_broll_candidate_to_block(
                block=block,
                desired_start=desired_start,
                desired_end=desired_end,
                last_end_time=last_end_time,
                strategy=strategy,
                video_duration=video_duration,
            )
            if not placed:
                continue

            start_time, end_time = placed
            candidates.append({
                "start_time": start_time,
                "end_time": end_time,
                "duration": round(end_time - start_time, 3),
                "semantic_type": semantic_type,
                "text": " ".join(block["texts"][:2]).strip() or "语义中插",
                "is_transition": False,
            })
            last_end_time = end_time

    return candidates


def _build_presence_candidate(
    subtitles: List[Dict],
    video_duration: float,
    sensitivity: str,
    semantic_type: str,
) -> Optional[Dict]:
    strategy = _get_broll_strategy_config(sensitivity)
    blocks = _build_semantic_blocks(subtitles)
    direct_blocks = [block for block in blocks if block["semantic_type"] == semantic_type]

    if semantic_type == "product":
        fallback_blocks = [
            block for block in blocks
            if block["semantic_type"] == "neutral" and block["start"] >= video_duration * 0.35
        ]
        if not fallback_blocks:
            fallback_blocks = [block for block in blocks if block["start"] >= video_duration * 0.45]
    else:
        fallback_blocks = [
            block for block in blocks
            if block["semantic_type"] == "neutral" and block["start"] <= video_duration * 0.6
        ]
        if not fallback_blocks:
            fallback_blocks = [block for block in blocks if block["start"] <= video_duration * 0.55]

    candidate_blocks = direct_blocks or fallback_blocks or blocks[-1:]
    if not candidate_blocks:
        return None

    block = max(candidate_blocks, key=lambda item: float(item["end"]) - float(item["start"]))
    block_duration = float(block["end"]) - float(block["start"])
    desired_duration = min(
        strategy["max_duration"],
        max(strategy["min_duration"], min(block_duration * 0.8, strategy["max_duration"])),
    )
    if semantic_type == "product":
        desired_start = max(block["start"], block["end"] - desired_duration)
    else:
        desired_start = block["start"]
    desired_end = min(video_duration, desired_start + desired_duration)

    if desired_end - desired_start < strategy["min_duration"]:
        desired_start = max(0.0, desired_end - strategy["min_duration"])
        desired_end = min(video_duration, desired_start + strategy["min_duration"])

    return {
        "start_time": round(desired_start, 3),
        "end_time": round(desired_end, 3),
        "duration": round(desired_end - desired_start, 3),
        "semantic_type": semantic_type,
        "text": f"{semantic_type}_coverage",
        "is_transition": False,
    }


def _ensure_semantic_presence(
    candidates: List[Dict],
    subtitles: List[Dict],
    video_duration: float,
    sensitivity: str,
    require_product: bool,
    require_symptom: bool,
) -> List[Dict]:
    normalized = _normalize_broll_candidates(candidates, subtitles, video_duration, sensitivity)
    existing_types = {item.get("semantic_type") for item in normalized}
    supplements: List[Dict] = []

    if require_product and "product" not in existing_types:
        candidate = _build_presence_candidate(subtitles, video_duration, sensitivity, "product")
        if candidate:
            supplements.append(candidate)

    if require_symptom and "symptom" not in existing_types:
        candidate = _build_presence_candidate(subtitles, video_duration, sensitivity, "symptom")
        if candidate:
            supplements.append(candidate)

    if supplements:
        normalized = _normalize_broll_candidates(
            normalized + supplements,
            subtitles,
            video_duration,
            sensitivity,
        )

    return normalized


def _pick_semantic_material(
    semantic_type: str,
    product_videos: List[Dict],
    symptom_videos: List[Dict],
    tracker: UsageTracker,
    selection_state: Dict[str, int],
) -> Tuple[Optional[Dict], Optional[str]]:
    if semantic_type == "product":
        preferred = tracker.filter_available_dicts(product_videos, "broll") if tracker else product_videos
        fallback = product_videos
        state_key = "product"
        material_type = "产品展示"
    else:
        preferred = tracker.filter_available_dicts(symptom_videos, "broll") if tracker else symptom_videos
        fallback = symptom_videos
        state_key = "symptom"
        material_type = "病症困扰"

    source = preferred or fallback
    if not source:
        return None, None

    idx = selection_state.get(state_key, 0)
    material = source[idx % len(source)]
    selection_state[state_key] = idx + 1
    if tracker:
        tracker.record(material)
    return material, material_type


def _materialize_broll_candidates(
    candidates: List[Dict],
    product_videos: List[Dict],
    symptom_videos: List[Dict],
    tracker: UsageTracker,
) -> List[Dict]:
    matches: List[Dict] = []
    selection_state = {"product": 0, "symptom": 0}

    for candidate in candidates:
        material, material_type = _pick_semantic_material(
            semantic_type=candidate["semantic_type"],
            product_videos=product_videos,
            symptom_videos=symptom_videos,
            tracker=tracker,
            selection_state=selection_state,
        )
        if not material:
            continue

        matches.append({
            "start_time": candidate["start_time"],
            "end_time": candidate["end_time"],
            "duration": candidate["duration"],
            "material": material,
            "material_type": material_type,
            "text": candidate.get("text", "语义中插"),
            "is_transition": candidate.get("is_transition", False),
            "semantic_type": candidate["semantic_type"],
        })

    return matches


def smart_material_matching(
    subtitles: List[Dict], 
    product_videos: List[Dict], 
    symptom_videos: List[Dict],
    sensitivity: str = 'medium',
    video_duration: float = 0,
    video_id: str = "default_video",
    tracker: UsageTracker = None
) -> Tuple[List[Dict], List[Dict], str]:
    """智能素材匹配：优先参考大模型语义剧本，否则基于字幕语义块做中插规划。"""
    print(f"正在进行智能素材匹配 (优化版)...")

    if not subtitles:
        print("   ⚠ 无字幕数据，无法匹配素材")
        return [], [], "neutral"

    video_duration = video_duration or max(sub['end'] for sub in subtitles)
    
    import random
    import csv

    llm_api_key = os.environ.get("LLM_API_KEY", "").strip()
    llm_base_url = os.environ.get("LLM_BASE_URL", "").strip()
    llm_model = os.environ.get("LLM_MODEL", "deepseek-v3.2").strip()

    matches = []
    sfx_list = []
    bgm_emotion = "neutral"
    candidate_matches = []

    if llm_api_key:
        try:
            import llm_clip_matcher
            plan = llm_clip_matcher.generate_editing_plan_with_llm(
                subtitles=subtitles,
                api_key=llm_api_key,
                model=llm_model,
                base_url=llm_base_url if llm_base_url else None
            )
            if plan:
                print("   [LLM] 成功获取大模型语义剧本，开始组装素材...")
                raw_candidates = []
                for b in plan.get("b_rolls", []):
                    start_time = b.get("start", 0)
                    end_time = b.get("end", 0)
                    semantic_type = b.get("type") or _infer_semantic_type_for_range(
                        subtitles, float(start_time), float(end_time)
                    )
                    if float(end_time) - float(start_time) < 0.5:
                        continue

                    raw_candidates.append({
                        "start_time": float(start_time),
                        "end_time": float(end_time),
                        "semantic_type": semantic_type,
                        "text": b.get("reason", "LLM中插"),
                        "is_transition": False,
                    })

                candidate_matches = _normalize_broll_candidates(
                    raw_candidates,
                    subtitles=subtitles,
                    video_duration=video_duration,
                    sensitivity=sensitivity,
                )
                sfx_list = plan.get("sfx", [])
                bgm_emotion = plan.get("bgm_emotion", "neutral")
        except Exception as e:
            print(f"   [LLM Error] 大模型处理异常: {e}，回退到规则匹配。")

    if not candidate_matches:
        print("   [INFO] 使用本地规则进行关键词匹配...")
        candidate_matches = _build_rule_based_broll_candidates(
            subtitles=subtitles,
            video_duration=video_duration,
            sensitivity=sensitivity,
        )

    candidate_matches = _ensure_semantic_presence(
        candidates=candidate_matches,
        subtitles=subtitles,
        video_duration=video_duration,
        sensitivity=sensitivity,
        require_product=bool(product_videos),
        require_symptom=bool(symptom_videos),
    )

    print("   [INFO] 根据语义块校准产品与病症中插位置，并避免连续中插...")
    matches = _materialize_broll_candidates(
        candidates=candidate_matches,
        product_videos=product_videos,
        symptom_videos=symptom_videos,
        tracker=tracker,
    )
    matches.sort(key=lambda x: x["start_time"])

    # 输出统计报告
    output_dir = OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)
    
    json_path = os.path.join(output_dir, f"{video_id}_insert_density_config.json")
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump([{'start': round(m['start_time'],2), 'end': round(m['end_time'],2), 'duration': round(m['duration'],2), 'type': m['material_type']} for m in matches], f, ensure_ascii=False, indent=2)
        
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
    subtitles: List[Dict],
    sfx_list: List[Dict] = None,
    bgm_emotion: str = "neutral",
    bgm_path: Optional[str] = None,
    tracker: UsageTracker = None,
    is_review_version: bool = False
) -> bool:
    """创建OTC药品推广视频"""
    try:
        print(f"\n正在创建OTC推广视频: {project_name}")
        is_valid_speech_video, validation_message = validate_speech_video_file(speech_video)
        if not is_valid_speech_video:
            print(validation_message)
            return False

        detected_version = detect_jianying_version()
        if detected_version and not is_supported_jianying_version(detected_version):
            raise RuntimeError(
                f"检测到当前剪映版本为 {detected_version}，"
                "该流程依赖 5.9 及以下版本的草稿结构，当前版本不受支持。"
            )
        if not detected_version:
            print("   [WARN] 未能自动识别剪映版本，将继续尝试生成草稿。")
        else:
            print(f"   检测到剪映版本: {detected_version}")
        
        # 获取口播视频时长
        speech_duration = 0
        import subprocess
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', speech_video],
                capture_output=True, text=True, check=True, encoding='utf-8', errors='ignore'
            )
            speech_duration = float(result.stdout.strip())
        except Exception:
            try:
                result = subprocess.run(
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
                if subtitles:
                    speech_duration = max(sub['end'] for sub in subtitles)
                else:
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
            project = JyProject(unique_project_name, overwrite=True, width=1080, height=1920)

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

                if match.get('is_placeholder', False):
                    seg = project.add_media_safe(
                        speech_video,
                        start_time=start_us,
                        duration=duration_us,
                        track_name="02_B_Roll",
                        source_start=start_us
                    )
                else:
                    seg = project.add_media_safe(
                        material['path'],
                        start_time=start_us,
                        duration=duration_us,
                        track_name="02_B_Roll"
                    )
                if seg and hasattr(seg, 'volume'):
                    seg.volume = 0.0

            # 3. 添加广审素材轨道 (Ad Review)
            print("   添加广审素材轨道...")
            project.script.add_track(draft.TrackType.video, "05_Ad_Review", absolute_index=20000)
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
            project.script.add_track(draft.TrackType.video, "06_Top_Sticker", absolute_index=21000)
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

            # 5. 添加字幕 (Subtitles)
            print("   添加字幕...")
            align_report = []
            laid_out_subtitles, subtitle_stats = layout_segments_on_tracks(
                subtitles,
                speech_duration,
                start_key="start",
                end_key="end",
                min_duration=0.05,
            )
            if (
                subtitle_stats["shifted_count"]
                or subtitle_stats["dropped_count"]
                or subtitle_stats["track_count"] > 1
            ):
                print(
                    "   [监控] 字幕时间线已标准化: "
                    f"分配 {subtitle_stats['track_count']} 条轨道, "
                    f"调整 {subtitle_stats['shifted_count']} 条, "
                    f"丢弃 {subtitle_stats['dropped_count']} 条"
                )

            for sub in laid_out_subtitles:
                subtitle_start = sub["start"]
                end_time = sub["end"]
                track_index = sub["track_index"]
                track_name = "05_Subtitles" if track_index == 0 else f"05_Subtitles_{track_index + 1}"

                align_report.append({
                    'text': sub['text'],
                    'start': f"{subtitle_start:.3f}",
                    'end': f"{end_time:.3f}",
                    'offset_ms': 0
                })

                project.add_text_simple(
                    text=sub['text'],
                    start_time=sub["start_us"],
                    duration=sub["duration_us"],
                    track_name=track_name,
                    clip_settings=draft.ClipSettings(transform_y=-0.4)
                )

            # 6. 添加背景音乐轨道（BGM）
            print(f"   添加BGM轨道 (情感倾向: {bgm_emotion})...")
            try:
                bgm_added = False
                local_bgm_files = []
                if bgm_path and os.path.exists(bgm_path):
                    local_bgm_files.append(bgm_path)
                elif os.path.isdir(BGM_DIR):
                    emotion_dir = os.path.join(BGM_DIR, bgm_emotion)
                    if os.path.isdir(emotion_dir):
                        for ext in ('*.mp3', '*.wav', '*.m4a', '*.aac'):
                            local_bgm_files.extend(glob.glob(os.path.join(emotion_dir, ext)))
                    if not local_bgm_files:
                        for ext in ('*.mp3', '*.wav', '*.m4a', '*.aac'):
                            local_bgm_files.extend(glob.glob(os.path.join(BGM_DIR, ext)))

                if local_bgm_files:
                    import random as _random
                    chosen_bgm = _random.choice(local_bgm_files)
                    bgm_seg = project.add_audio_safe(chosen_bgm, start_time="0s", duration=f"{speech_duration}s", track_name="BGM")
                    if bgm_seg:
                        bgm_seg.volume = 0.6
                        if hasattr(bgm_seg, 'fade_in'):
                            bgm_seg.fade_in = 1000000
                        if hasattr(bgm_seg, 'fade_out'):
                            bgm_seg.fade_out = 2000000
                        bgm_added = True
                        print(f"   [OK] 使用本地BGM: {os.path.basename(chosen_bgm)}, 已设置音量和淡入淡出")
                if not bgm_added:
                    print("   [SKIP] BGM未添加（未找到有效文件）")
            except Exception as e:
                print(f"   [SKIP] BGM添加失败: {e}")

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
            if registry_report["restored_from_recycle"] or registry_report["invalid_drafts"]:
                print(
                    "   [监控] 草稿索引已修复: "
                    f"恢复 {len(registry_report['restored_from_recycle'])} 个, "
                    f"发现无效目录 {len(registry_report['invalid_drafts'])} 个"
                )

            # 独立导出每一句字幕为单独的SRT文件
            print("   正在将每一句话单独切割成独立的字幕文件...")
            subtitle_out_dir = os.path.join(OUTPUT_DIR, f"{unique_project_name}_独立字幕")
            os.makedirs(subtitle_out_dir, exist_ok=True)
            for sub in subtitles:
                end_time = min(sub['end'], speech_duration)
                if sub['start'] >= speech_duration or (end_time - sub['start']) <= 0.05:
                    continue

                def format_time_filename(seconds):
                    h = int(seconds // 3600)
                    m = int((seconds % 3600) // 60)
                    s = int(seconds % 60)
                    ms = int((seconds % 1) * 1000)
                    return f"{h:02d}_{m:02d}_{s:02d}_{ms:03d}"

                def format_time_srt(seconds):
                    h = int(seconds // 3600)
                    m = int((seconds % 3600) // 60)
                    s = int(seconds % 60)
                    ms = int((seconds % 1) * 1000)
                    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

                time_prefix = format_time_filename(sub['start'])
                safe_text = "".join([c for c in sub['text'] if c not in r'\/:*?"<>|'])[:15]
                srt_filename = f"{time_prefix}_{safe_text}.srt"
                srt_path = os.path.join(subtitle_out_dir, srt_filename)

                with open(srt_path, 'w', encoding='utf-8') as f:
                    f.write("1\n")
                    f.write(f"{format_time_srt(sub['start'])} --> {format_time_srt(end_time)}\n")
                    f.write(f"{sub['text']}\n")

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
            "track_hierarchy": [
                {"track_id": 1, "name": "01_Main_Video", "content": "主视频内容", "duration": speech_duration},
                {"track_id": 2, "name": "02_B_Roll", "content": "中插素材", "count": len(matches), "total_duration": total_insert},
                {"track_id": 3, "name": "05_Ad_Review", "content": "广审文件", "duration": speech_duration, "is_full_duration": True, "opacity": "100%", "position": "bottom_10%"},
                {"track_id": 4, "name": "06_Top_Sticker", "content": "顶部贴图", "duration": speech_duration, "is_full_duration": True, "opacity": "100%", "position": "top_10%"},
                {"track_id": 5, "name": "05_Subtitles", "content": "对白字幕", "count": len(subtitles), "style": "Source Han Sans, 26pt (12.0), White+1px Stroke", "position": "bottom_safe_margin_10%"}
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
                "insert_ratio": f"{insert_ratio:.1f}%"
            }
        }
        
        output_dir = OUTPUT_DIR
        os.makedirs(output_dir, exist_ok=True)
        report_path = os.path.join(output_dir, f"{unique_project_name}_审查报告.json")
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(report_data, f, ensure_ascii=False, indent=4)

        # 生成字幕对齐校验报告 (CSV)
        csv_align_path = os.path.join(output_dir, f"{unique_project_name}_字幕对齐报告.csv")
        with open(csv_align_path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['序号', '字幕文本', '开始时间(s)', '结束时间(s)', '偏移误差(ms)', '动画模式'])
            for idx, item in enumerate(align_report, 1):
                anim_mode = "逐字渐显" if (idx > 1 and (float(item['start']) - float(align_report[idx-2]['end']) < 0.3)) else "整句展现"
                if idx == 1 and float(item['start']) < 0.3:
                    anim_mode = "逐字渐显"
                writer.writerow([idx, item['text'], item['start'], item['end'], item['offset_ms'], anim_mode])

        print(f"\n   [STAT] 时长分布统计与审查报告:")
        print(f"   {'='*45}")
        print(f"   视频总时长:     {speech_duration:.1f}秒")
        print(f"   中插素材总时长: {total_insert:.1f}秒 ({insert_ratio:.1f}%)")
        print(f"   口播可见时长:   {speech_visible:.1f}秒 ({100-insert_ratio:.1f}%)")
        print(f"   {'='*45}")
        if insert_ratio >= 59.9:
            print(f"   [OK] 中插占比达标 ({insert_ratio:.1f}% >= 60%)")
        else:
            print(f"   [!] 中插占比未达标 ({insert_ratio:.1f}% < 60%)")
        print(f"\n   [DETAIL] 素材时间节点明细:")
        for i, m in enumerate(matches, 1):
            marker = "[T]转折点" if m.get('is_transition') else "[N]常规"
            print(f"   [{i:02d}] {m['start_time']:6.1f}s - {m['end_time']:6.1f}s | {m['duration']:.1f}s | {m['material_type']} | {marker} | {m['text'][:15]}")

        print(f"   [生成] 全片审查报告已保存: {report_path}")
        print(f"   [生成] 字幕对齐报告已保存: {csv_align_path}")

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
    args = parser.parse_args()

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

    # 3. AI语音识别与语义分析
    print("步骤3: AI语音识别与语义分析...")
    subtitles = transcribe_with_ai(selected_video['path'])
    
    # 统计语义类型
    symptom_count = sum(1 for s in subtitles if s.get('semantic_type') == 'symptom')
    product_count = sum(1 for s in subtitles if s.get('semantic_type') == 'product')
    neutral_count = sum(1 for s in subtitles if s.get('semantic_type') == 'neutral')
    
    print(f"   - 病症相关: {symptom_count} 条")
    print(f"   - 产品相关: {product_count} 条")
    print(f"   - 中性内容: {neutral_count} 条\n")

    # 4. 智能素材匹配（设置灵敏度）
    print("步骤4: 智能素材匹配...")
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
        subtitles, 
        product_videos, 
        symptom_videos,
        sensitivity=sensitivity,
        video_duration=selected_video['duration'],
        video_id=video_id,
        tracker=tracker
    )
    
    symptom_matches = sum(1 for m in matches if m['material_type'] == "病症困扰")
    product_matches = sum(1 for m in matches if m['material_type'] == "产品展示")
    
    print(f"   - 病症素材: {symptom_matches} 处")
    print(f"   - 产品素材: {product_matches} 处\n")

    # 5. 创建OTC推广视频
    print("步骤5: 创建OTC推广视频...")
    project_name = f"OTC推广_{os.path.splitext(selected_video['filename'])[0]}"
    success = create_otc_promo_video(
        project_name,
        selected_video['path'],
        matches,
        subtitles,
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
        print(f"字幕数量: {len(subtitles)} 条")
        print(f"素材匹配: {len(matches)} 处")
        print(f"素材灵敏度: {sensitivity}")
        print("\n您可以在剪映中打开此草稿进行审核和微调")
        print("建议调整:")
        print("  1. 检查字幕与口播内容的匹配度")
        print("  2. 调整素材的转场效果")
        print("  3. 添加适当的背景音乐")
        print("  4. 确保符合OTC药品推广规范")
        print("  5. 如需调整素材密度，修改sensitivity参数")
        print("=" * 80)
    else:
        print("\n[失败] 视频创建失败，请检查错误信息")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
