"""Whisper 语音转写模块 — 提取口播视频音频并逐句转写。"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
from typing import Dict, List, Optional

from app_paths import resource_path

_WHISPER_MODEL = None
_WHISPER_MODEL_LOCK = threading.Lock()


def _resolve_ffmpeg() -> str:
    """查找 FFmpeg 路径（优先系统 PATH，其次 bundled）。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    bundled = resource_path("ffmpeg-8.1-essentials_build", "bin", "ffmpeg.exe")
    if os.path.isfile(bundled):
        return bundled
    return "ffmpeg"


def _load_whisper_model() -> Optional[object]:
    """懒加载 Whisper base 模型（单例，线程安全）。"""
    global _WHISPER_MODEL
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL
    with _WHISPER_MODEL_LOCK:
        if _WHISPER_MODEL is not None:
            return _WHISPER_MODEL
        try:
            import whisper
            _WHISPER_MODEL = whisper.load_model("base")
            print("   [Whisper] base 模型加载完成")
        except ImportError:
            print("   [Whisper] openai-whisper 未安装，转写功能不可用")
            _WHISPER_MODEL = False
        except Exception as exc:
            print(f"   [Whisper] 模型加载失败: {exc}")
            _WHISPER_MODEL = False
    return _WHISPER_MODEL if _WHISPER_MODEL is not False else None


def extract_audio_from_video(video_path: str) -> Optional[str]:
    """从视频提取 16kHz mono WAV 音频，返回临时文件路径。"""
    ffmpeg = _resolve_ffmpeg()
    fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="whisper_audio_")
    os.close(fd)
    try:
        subprocess.run(
            [
                ffmpeg, "-i", video_path,
                "-vn", "-acodec", "pcm_s16le",
                "-ar", "16000", "-ac", "1",
                "-y", tmp_path,
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
        if os.path.getsize(tmp_path) > 1024:
            return tmp_path
        print("   [Whisper] 提取的音频文件过小，可能视频无音轨")
        return None
    except subprocess.CalledProcessError as exc:
        print(f"   [Whisper] FFmpeg 提取音频失败: {exc.stderr.decode('utf-8', errors='replace')[:200]}")
        return None
    except Exception as exc:
        print(f"   [Whisper] 音频提取异常: {exc}")
        return None


def _transcript_cache_dir(output_dir: str) -> str:
    d = os.path.join(output_dir, "transcriptions")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(video_name: str, cache_dir: str) -> str:
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in video_name)
    return os.path.join(cache_dir, f"{safe_name}.json")


def read_cached_transcript(video_name: str, cache_dir: str) -> Optional[List[Dict]]:
    path = _cache_path(video_name, cache_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        segments = data.get("segments", [])
        if segments and all(isinstance(s, dict) and "start" in s and "end" in s and "text" in s for s in segments):
            return segments
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return None


def write_transcript(video_name: str, segments: List[Dict], cache_dir: str) -> None:
    path = _cache_path(video_name, cache_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"segments": segments}, f, ensure_ascii=False, indent=2)


def transcribe_speech(video_path: str, video_name: str, cache_dir: str) -> Optional[List[Dict]]:
    """转写口播视频为逐句时间戳，带缓存。

    返回: [{start: float, end: float, text: str}, ...] 或 None
    """
    cached = read_cached_transcript(video_name, cache_dir)
    if cached is not None:
        print(f"   [Whisper] 命中缓存 ({len(cached)} 句)")
        return cached

    model = _load_whisper_model()
    if model is None:
        return None

    audio_path = extract_audio_from_video(video_path)
    if audio_path is None:
        return None

    try:
        print("   [Whisper] 正在转写口播内容...")
        result = model.transcribe(audio_path, language="zh", verbose=False)
        segments = []
        for seg in result.get("segments", []):
            segments.append({
                "start": round(float(seg.get("start", 0)), 3),
                "end": round(float(seg.get("end", 0)), 3),
                "text": str(seg.get("text", "")).strip(),
            })
        if segments:
            write_transcript(video_name, segments, cache_dir)
            print(f"   [Whisper] 转写完成 ({len(segments)} 句)")
            return segments
        print("   [Whisper] 转写结果为空")
        return None
    except Exception as exc:
        print(f"   [Whisper] 转写异常: {exc}")
        return None
    finally:
        try:
            os.remove(audio_path)
        except OSError:
            pass
