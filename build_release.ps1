$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ProjectRoot ".venv_release\Scripts\python.exe"
$PyInstallerExe = Join-Path $ProjectRoot ".venv_release\Scripts\pyinstaller.exe"
$SpecFile = Join-Path $ProjectRoot "YunFengEditor.spec"
$BuildStamp = Get-Date -Format "yyyyMMdd_HHmmss"
$DistDir = Join-Path $ProjectRoot "dist"
$BuildDir = Join-Path $ProjectRoot "build"
$ReleaseAssetsRoot = Join-Path $ProjectRoot ".release_assets"
$WhisperAssetDir = Join-Path $ReleaseAssetsRoot "whisper"
$LocalFfmpegRoot = Join-Path $ProjectRoot "ffmpeg-8.1-essentials_build"
$Env:OTC_RELEASE_SKILL_ROOT = Join-Path $ProjectRoot "jianying-editor-skill-main\jianying-editor-skill-main"

if (-not (Test-Path $PythonExe)) {
    throw "Release Python not found: $PythonExe"
}

if (-not (Test-Path $PyInstallerExe)) {
    throw "PyInstaller not found: $PyInstallerExe"
}

if (-not (Test-Path $Env:OTC_RELEASE_SKILL_ROOT)) {
    throw "Skill root not found: $Env:OTC_RELEASE_SKILL_ROOT"
}

if (Test-Path $LocalFfmpegRoot) {
    $Env:OTC_RELEASE_FFMPEG_ROOT = $LocalFfmpegRoot
} elseif (-not $Env:OTC_RELEASE_FFMPEG_ROOT) {
    $FfmpegCmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($FfmpegCmd) {
        $BinDir = Split-Path $FfmpegCmd.Source -Parent
        $CandidateRoot = Split-Path $BinDir -Parent
        if (Test-Path (Join-Path $CandidateRoot "bin\ffmpeg.exe")) {
            $Env:OTC_RELEASE_FFMPEG_ROOT = $CandidateRoot
        }
    }
}

if (-not $Env:OTC_RELEASE_FFMPEG_ROOT -or -not (Test-Path $Env:OTC_RELEASE_FFMPEG_ROOT)) {
    throw "FFmpeg root not found. Set OTC_RELEASE_FFMPEG_ROOT or place ffmpeg under $LocalFfmpegRoot"
}

New-Item -ItemType Directory -Path $WhisperAssetDir -Force | Out-Null
$Env:OTC_RELEASE_WHISPER_MODEL = Join-Path $WhisperAssetDir "base.pt"
if (-not (Test-Path $Env:OTC_RELEASE_WHISPER_MODEL)) {
    Write-Host "==> Prepare Whisper base model"
    & $PythonExe -c "import whisper, pathlib; target = pathlib.Path(r'$WhisperAssetDir'); target.mkdir(parents=True, exist_ok=True); whisper.load_model('base', download_root=str(target))"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to prepare Whisper base model"
    }
}

Write-Host "==> Clean old build folders"
try {
    if (Test-Path $BuildDir) {
        Remove-Item $BuildDir -Recurse -Force
    }
    if (Test-Path $DistDir) {
        Remove-Item $DistDir -Recurse -Force
    }
} catch {
    $BuildDir = Join-Path $ProjectRoot ("build_" + $BuildStamp)
    $DistDir = Join-Path $ProjectRoot ("dist_" + $BuildStamp)
    Write-Host "==> Existing release directory is in use, switch to $DistDir"
    if (Test-Path $BuildDir) {
        Remove-Item $BuildDir -Recurse -Force
    }
    if (Test-Path $DistDir) {
        Remove-Item $DistDir -Recurse -Force
    }
}

Write-Host "==> Build one-folder release"
& $PyInstallerExe --clean --noconfirm --distpath $DistDir --workpath $BuildDir $SpecFile
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

$ReleaseDir = Join-Path $DistDir "YunFengEditor"

Write-Host "==> Run release verification"
& $PythonExe (Join-Path $ProjectRoot "verify_release.py") --dist-root $ReleaseDir
if ($LASTEXITCODE -ne 0) {
    throw "Release verification failed with exit code $LASTEXITCODE"
}

Copy-Item (Join-Path $ProjectRoot "RELEASE_GUIDE.md") (Join-Path $ReleaseDir "RELEASE_GUIDE.md") -Force

$ZipPath = Join-Path $DistDir "YunFengEditor-portable-win64.zip"
if (Test-Path $ZipPath) {
    Remove-Item $ZipPath -Force
}
Compress-Archive -Path (Join-Path $ReleaseDir "*") -DestinationPath $ZipPath -Force

Write-Host "==> Build finished: $ReleaseDir"
