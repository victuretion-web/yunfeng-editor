import os
from typing import List, Tuple


SUPPORTED_VIDEO_EXTENSIONS = (".mp4", ".mov", ".avi", ".wmv", ".mkv", ".m4v")
SUPPORTED_AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")
GENERATED_ARTIFACT_KEYWORDS = ("OTC推广", "干净版", "审查版")


def get_lower_extension(path: str) -> str:
    return os.path.splitext(str(path))[1].lower()


def is_supported_video_file(path: str) -> bool:
    return get_lower_extension(path) in SUPPORTED_VIDEO_EXTENSIONS


def is_audio_file(path: str) -> bool:
    return get_lower_extension(path) in SUPPORTED_AUDIO_EXTENSIONS


def should_skip_generated_artifact(filename: str) -> bool:
    name = str(filename)
    return any(keyword in name for keyword in GENERATED_ARTIFACT_KEYWORDS)


def scan_video_file_paths(
    directory: str,
    recursive: bool = True,
    skip_generated_artifacts: bool = True,
) -> Tuple[List[str], List[str]]:
    video_paths: List[str] = []
    skipped_audio_paths: List[str] = []

    if not directory or not os.path.exists(directory):
        return video_paths, skipped_audio_paths

    walker = os.walk(directory) if recursive else [(directory, [], os.listdir(directory))]
    for root, _, files in walker:
        for filename in sorted(files):
            if skip_generated_artifacts and should_skip_generated_artifact(filename):
                continue

            full_path = os.path.join(root, filename)
            if is_supported_video_file(full_path):
                video_paths.append(full_path)
            elif is_audio_file(full_path):
                skipped_audio_paths.append(full_path)

    video_paths.sort(key=lambda item: item.lower())
    skipped_audio_paths.sort(key=lambda item: item.lower())
    return video_paths, skipped_audio_paths


def validate_speech_video_file(path: str) -> Tuple[bool, str]:
    if is_supported_video_file(path):
        return True, ""
    if is_audio_file(path):
        return False, f"[SKIP] 检测到音频口播文件，已跳过: {os.path.basename(path)}"
    return False, f"[SKIP] 不支持的口播文件格式，已跳过: {os.path.basename(path)}"
