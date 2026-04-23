$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ProjectRoot ".venv_release\Scripts\python.exe"
$PyInstallerExe = Join-Path $ProjectRoot ".venv_release\Scripts\pyinstaller.exe"
$SpecFile = Join-Path $ProjectRoot "YunFengEditor.spec"

if (-not (Test-Path $PythonExe)) {
    throw "Release Python not found: $PythonExe"
}

if (-not (Test-Path $PyInstallerExe)) {
    throw "PyInstaller not found: $PyInstallerExe"
}

Write-Host "==> Clean old build folders"
$BuildDir = Join-Path $ProjectRoot "build"
$DistDir = Join-Path $ProjectRoot "dist"
if (Test-Path $BuildDir) {
    Remove-Item $BuildDir -Recurse -Force
}
if (Test-Path $DistDir) {
    Remove-Item $DistDir -Recurse -Force
}

Write-Host "==> Build one-folder release"
& $PyInstallerExe --clean --noconfirm $SpecFile
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

Write-Host "==> Run release verification"
& $PythonExe (Join-Path $ProjectRoot "verify_release.py")
if ($LASTEXITCODE -ne 0) {
    throw "Release verification failed with exit code $LASTEXITCODE"
}

$ReleaseDir = Join-Path $DistDir "YunFengEditor"
Copy-Item (Join-Path $ProjectRoot "RELEASE_GUIDE.md") (Join-Path $ReleaseDir "RELEASE_GUIDE.md") -Force

$ZipPath = Join-Path $DistDir "YunFengEditor-portable-win64.zip"
if (Test-Path $ZipPath) {
    Remove-Item $ZipPath -Force
}
Compress-Archive -Path (Join-Path $ReleaseDir "*") -DestinationPath $ZipPath -Force

Write-Host "==> Build finished: $ReleaseDir"
