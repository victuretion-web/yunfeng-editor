# YunFengEditor Portable Release Guide

## 1. Deliverables

- Release directory: `dist/YunFengEditor/`
- Main executable: `dist/YunFengEditor/YunFengEditor.exe`
- Verification report: `dist/YunFengEditor/release_verification.json`
- Runtime output directory: `output/` will be created next to the executable after first launch

This release uses the Windows `PyInstaller one-folder` portable packaging mode.

## 2. System Requirements

- Windows 10 or Windows 11 64-bit
- No separate Python installation required
- Jianying Professional `5.9.x` or lower compatible version
- Recommended free disk space: at least `6 GB`
- Recommended memory: `16 GB` or higher

## 3. Installation

1. Extract `YunFengEditor-portable-win64.zip` to a local disk folder.
2. Make sure the extracted folder contains:
   - `YunFengEditor.exe`
   - `_internal/`
   - `release_verification.json`
   - `RELEASE_GUIDE.md`
3. Double-click `YunFengEditor.exe`.

## 4. Built-in Resources

- FFmpeg executables
- Whisper `base` model
- Jianying skill runtime scripts
- pyJianYingDraft template assets
- GUI launcher and worker entry

## 5. Validation Performed

- Clean release virtual environment dependency installation
- PyInstaller portable build
- `YunFengEditor.exe --worker --help` smoke check
- `YunFengEditor.exe --worker --preflight` runtime self-check
- GUI startup 8-second smoke check
- Auto-generated sample media and one real draft generation smoke run
- Required packaged resource existence check

## 6. Notes

- Do not move files out of `_internal/`
- Do not distribute only the single exe file
- API Key is no longer hardcoded; enter it once in the UI and it will be saved locally in `output/ui_settings.json`
- If repackaging is needed, run `build_release.ps1`
