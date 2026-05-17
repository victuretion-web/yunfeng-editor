import os
from typing import Dict, Tuple

import numpy as np

try:
    import pyaudioop  # Ensure PyInstaller collects the Python 3.13 audioop shim.
except ImportError:
    pyaudioop = None

try:
    from pydub import AudioSegment
except ImportError:
    class AudioSegment:  # type: ignore[no-redef]
        @staticmethod
        def from_file(_path: str):
            raise RuntimeError("pydub 未安装，无法处理背景音乐")

from subprocess_windows import run_hidden


def _safe_crossfade_ms(processed: AudioSegment, source: AudioSegment, requested_ms: int) -> int:
    return max(0, min(int(requested_ms), len(processed) // 4, len(source) // 4, 1000))


def _compute_phase_report(audio: AudioSegment) -> Dict[str, object]:
    if audio.channels < 2:
        return {"checked": False, "correlation": None, "status": "单声道"}

    samples = np.array(audio.get_array_of_samples())
    try:
        frame_samples = samples.reshape((-1, audio.channels))
    except ValueError:
        return {"checked": False, "correlation": None, "status": "采样异常"}

    left = frame_samples[:, 0].astype(np.float64)
    right = frame_samples[:, 1].astype(np.float64)
    if np.std(left) < 1e-6 or np.std(right) < 1e-6:
        return {"checked": True, "correlation": None, "status": "能量过低"}

    correlation = float(np.corrcoef(left, right)[0, 1])
    status = "正常"
    if correlation < -0.2:
        status = "疑似反相"
    elif correlation < 0.15:
        status = "偏弱"
    return {"checked": True, "correlation": round(correlation, 4), "status": status}


def _normalize_loudness(input_path: str, output_path: str, target_lufs: int) -> Tuple[bool, str]:
    result = run_hidden(
        [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-af",
            f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11",
            output_path,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    stderr_text = str(result.stderr or "").strip()
    if result.returncode != 0:
        message = stderr_text[-400:] if stderr_text else f"ffmpeg 返回码 {result.returncode}"
        return False, f"loudnorm 执行失败: {message}"
    if not os.path.exists(output_path):
        return False, "loudnorm 未生成输出文件"
    lowered = stderr_text.lower()
    if "error" in lowered or "failed" in lowered or "invalid" in lowered:
        return False, f"loudnorm 输出包含异常: {stderr_text[-400:]}"
    return True, ""


def prepare_bgm_for_timeline(
    bgm_path: str,
    target_duration_sec: float,
    output_dir: str,
    prefix: str,
    crossfade_ms: int = 200,
    target_lufs: int = -16,
    normalize_lufs: bool = True,
    phase_check: bool = True,
) -> Tuple[str, Dict[str, object]]:
    try:
        audio = AudioSegment.from_file(bgm_path)
    except Exception as exc:
        raise RuntimeError(f"背景音乐读取失败: {os.path.basename(bgm_path)} ({exc})") from exc
    audio = audio.set_channels(2).set_frame_rate(48000)
    target_ms = max(100, int(round(float(target_duration_sec) * 1000)))

    processing_mode = "clip"
    if len(audio) >= target_ms:
        processed = audio[:target_ms]
    else:
        processing_mode = "loop"
        processed = audio
        while len(processed) < target_ms:
            crossfade = _safe_crossfade_ms(processed, audio, crossfade_ms)
            processed = processed.append(audio, crossfade=crossfade)
        processed = processed[:target_ms]

    phase_report = _compute_phase_report(processed) if phase_check else {"checked": False, "correlation": None, "status": "关闭"}

    os.makedirs(output_dir, exist_ok=True)
    rendered_path = os.path.join(output_dir, f"{prefix}_bgm_prepared.wav")
    try:
        processed.export(rendered_path, format="wav")
    except Exception as exc:
        raise RuntimeError(f"背景音乐导出失败: {os.path.basename(rendered_path)} ({exc})") from exc

    final_path = rendered_path
    normalization_warning = ""
    if normalize_lufs:
        normalized_path = os.path.join(output_dir, f"{prefix}_bgm_lufs.wav")
        normalized_ok, normalization_warning = _normalize_loudness(rendered_path, normalized_path, target_lufs=target_lufs)
        if normalized_ok:
            final_path = normalized_path

    report = {
        "source_path": bgm_path,
        "output_path": final_path,
        "source_duration_sec": round(len(audio) / 1000.0, 3),
        "target_duration_sec": round(target_duration_sec, 3),
        "mode": processing_mode,
        "crossfade_ms": int(crossfade_ms),
        "target_lufs": int(target_lufs),
        "normalized": bool(normalize_lufs),
        "normalization_warning": normalization_warning,
        "phase_report": phase_report,
    }
    return final_path, report
