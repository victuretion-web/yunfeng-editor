# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project_root = Path(SPECPATH).resolve()
dist_name = "YunFengEditor"

skill_root = project_root / "jianying-editor-skill-main" / "jianying-editor-skill-main"
ffmpeg_root = project_root / "ffmpeg-8.1-essentials_build"
whisper_model = project_root / ".whisper_cache" / "base.pt"
subtitle_panel = project_root / "subtitle_sync_panel.html"

datas = [
    (str(skill_root), "jianying-editor-skill-main/jianying-editor-skill-main"),
    (str(ffmpeg_root), "ffmpeg-8.1-essentials_build"),
]

if whisper_model.exists():
    datas.append((str(whisper_model), ".whisper_cache"))

if subtitle_panel.exists():
    datas.append((str(subtitle_panel), "."))

datas += collect_data_files("whisper")
datas += collect_data_files("tiktoken")
datas += collect_data_files("imageio")

hiddenimports = [
    "llm_clip_matcher",
    "edge_tts",
    "pymediainfo",
    "requests",
    "uiautomation",
]
hiddenimports += collect_submodules("whisper")
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
