import os
import sys
from typing import Dict, Iterable, List, Optional

SKILL_RELATIVE_PATH = ("jianying-editor-skill-main", "jianying-editor-skill-main")


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def get_resource_base_dir() -> str:
    if is_frozen():
        return os.path.abspath(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


def get_runtime_base_dir() -> str:
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(*parts: str) -> str:
    return os.path.join(get_resource_base_dir(), *parts)


def runtime_path(*parts: str) -> str:
    return os.path.join(get_runtime_base_dir(), *parts)


def get_skill_root() -> str:
    return resource_path(*SKILL_RELATIVE_PATH)


def get_output_dir() -> str:
    return runtime_path("output")


def get_runtime_whisper_cache_dir() -> str:
    return runtime_path(".whisper_cache")


def get_bundled_whisper_model_path(model_name: str) -> str:
    return resource_path(".whisper_cache", f"{model_name}.pt")


def ensure_skill_scripts_on_path() -> str:
    skill_root = get_skill_root()
    marker_file = os.path.join(skill_root, "scripts", "jy_wrapper.py")
    if not os.path.exists(marker_file):
        raise ImportError(f"Could not find jianying-editor skill root: {skill_root}")

    candidate_paths = [
        os.path.join(skill_root, "scripts"),
        os.path.join(skill_root, "scripts", "utils"),
        os.path.join(skill_root, "examples"),
    ]
    for candidate in reversed(candidate_paths):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
    return skill_root


def ensure_runtime_directories() -> None:
    os.makedirs(get_output_dir(), exist_ok=True)
    os.makedirs(get_runtime_whisper_cache_dir(), exist_ok=True)


def get_launcher_script_path() -> str:
    return os.path.join(get_runtime_base_dir(), "app_launcher.py")


def get_worker_command(extra_args: Optional[Iterable[str]] = None) -> List[str]:
    command: List[str]
    if is_frozen():
        command = [sys.executable, "--worker"]
    else:
        command = [sys.executable, get_launcher_script_path(), "--worker"]
    if extra_args:
        command.extend(extra_args)
    return command


def build_runtime_env(base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(base_env or os.environ)
    ffmpeg_bin = resource_path("ffmpeg-8.1-essentials_build", "bin")
    if os.path.isdir(ffmpeg_bin):
        env["PATH"] = ffmpeg_bin + os.pathsep + env.get("PATH", "")
    env.setdefault("OTC_APP_RESOURCE_DIR", get_resource_base_dir())
    env.setdefault("OTC_APP_RUNTIME_DIR", get_runtime_base_dir())
    env.setdefault("OTC_OUTPUT_DIR", get_output_dir())
    env.setdefault("OTC_WHISPER_CACHE_DIR", get_runtime_whisper_cache_dir())
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def configure_current_process() -> Dict[str, str]:
    env = build_runtime_env(os.environ)
    os.environ.update(env)
    ensure_runtime_directories()
    return env
