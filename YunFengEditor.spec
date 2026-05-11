# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project_root = Path(SPECPATH).resolve()
dist_name = "YunFengEditor"

release_assets_root = project_root / ".release_assets"


def resolve_path(env_name: str, default_path: Path, description: str, required: bool = True) -> Path:
    candidate = Path(os.environ.get(env_name, "")).expanduser() if os.environ.get(env_name) else default_path
    candidate = candidate.resolve()
    if required and not candidate.exists():
        raise SystemExit(f"Missing required release asset for {description}: {candidate}")
    return candidate


skill_root = resolve_path(
    "OTC_RELEASE_SKILL_ROOT",
    project_root / "jianying-editor-skill-main" / "jianying-editor-skill-main",
    "skill root",
)
ffmpeg_root = resolve_path(
    "OTC_RELEASE_FFMPEG_ROOT",
    project_root / "ffmpeg-8.1-essentials_build",
    "ffmpeg root",
)
datas = [
    (str(skill_root), "jianying-editor-skill-main/jianying-editor-skill-main"),
    (str(ffmpeg_root), "ffmpeg-8.1-essentials_build"),
]


datas += collect_data_files("tiktoken")
datas += collect_data_files("imageio")

hiddenimports = [
    "llm_clip_matcher",
    "audioop",
    "bgm_pipeline",
    "edge_tts",
    "insert_density",
    "pyaudioop",
    "pymediainfo",
    "requests",
    "semantic_matcher",
    "uiautomation",
]

hiddenimports += collect_submodules("tiktoken")
hiddenimports += collect_submodules("pynput")

excludes = [
    "pytest",
    "IPython",
    "jupyter",
    "notebook",
    "matplotlib",
]


a = Analysis(
    ["app_launcher.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=dist_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=dist_name,
)
