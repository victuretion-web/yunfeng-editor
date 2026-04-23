import json
import os
import subprocess
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DIST_ROOT = PROJECT_ROOT / "dist" / "YunFengEditor"
INTERNAL_ROOT = DIST_ROOT / "_internal"
EXE_PATH = DIST_ROOT / "YunFengEditor.exe"
REPORT_PATH = DIST_ROOT / "release_verification.json"


def check_exists(path: Path) -> dict:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir() if path.exists() else False,
    }


def run_worker_help() -> dict:
    completed = subprocess.run(
        [str(EXE_PATH), "--worker", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return {
        "returncode": completed.returncode,
        "stdout_head": completed.stdout[:2000],
        "stderr_head": completed.stderr[:2000],
        "ok": completed.returncode == 0 and "OTC" in completed.stdout,
    }


def run_gui_smoke_test() -> dict:
    process = subprocess.Popen(
        [str(EXE_PATH)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(DIST_ROOT),
    )
    time.sleep(8)
    still_running = process.poll() is None
    result = {
        "started": True,
        "still_running_after_8s": still_running,
        "returncode": process.poll(),
    }

    if still_running:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)
        result["terminated_for_smoke_test"] = True
    else:
        result["terminated_for_smoke_test"] = False

    return result


def main() -> int:
    report = {
        "dist_root": str(DIST_ROOT),
        "checks": {
            "exe": check_exists(EXE_PATH),
            "ffmpeg": check_exists(INTERNAL_ROOT / "ffmpeg-8.1-essentials_build" / "bin" / "ffmpeg.exe"),
            "ffprobe": check_exists(INTERNAL_ROOT / "ffmpeg-8.1-essentials_build" / "bin" / "ffprobe.exe"),
            "skill_wrapper": check_exists(
                INTERNAL_ROOT
                / "jianying-editor-skill-main"
                / "jianying-editor-skill-main"
                / "scripts"
                / "jy_wrapper.py"
            ),
            "whisper_base_model": check_exists(INTERNAL_ROOT / ".whisper_cache" / "base.pt"),
            "subtitle_panel": check_exists(INTERNAL_ROOT / "subtitle_sync_panel.html"),
        },
        "worker_help": None,
        "gui_smoke_test": None,
        "ok": False,
    }

    if not EXE_PATH.exists():
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[失败] 未找到发布程序: {EXE_PATH}")
        return 1

    report["worker_help"] = run_worker_help()
    report["gui_smoke_test"] = run_gui_smoke_test()

    required_keys = ["exe", "ffmpeg", "ffprobe", "skill_wrapper"]
    required_ok = all(report["checks"][key]["exists"] for key in required_keys)
    report["ok"] = (
        required_ok
        and report["worker_help"]["ok"]
        and report["gui_smoke_test"]["still_running_after_8s"]
    )

    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[报告] 发布校验报告已写入: {REPORT_PATH}")
    print(f"[结果] {'通过' if report['ok'] else '失败'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
