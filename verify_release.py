import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def check_exists(path: Path) -> dict:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir() if path.exists() else False,
    }


def run_worker_help(exe_path: Path) -> dict:
    completed = subprocess.run(
        [str(exe_path), "--worker", "--help"],
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


def run_worker_preflight(exe_path: Path, dist_root: Path) -> dict:
    completed = subprocess.run(
        [str(exe_path), "--worker", "--preflight"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(dist_root),
        timeout=120,
    )
    payload = {}
    try:
        payload = json.loads(completed.stdout.strip()) if completed.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    fatal_errors = payload.get("fatal_errors", [])
    return {
        "returncode": completed.returncode,
        "stdout_head": completed.stdout[:3000],
        "stderr_head": completed.stderr[:2000],
        "payload": payload,
        "ok": completed.returncode in (0, 1) and not fatal_errors,
    }


def run_gui_smoke_test(exe_path: Path, dist_root: Path) -> dict:
    process = subprocess.Popen(
        [str(exe_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(dist_root),
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


def create_sample_video(ffmpeg_path: Path, output_path: Path, color: str, duration: float) -> None:
    cmd = [
        str(ffmpeg_path),
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=720x1280:d={duration}",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=16000:cl=mono",
        "-shortest",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(output_path),
    ]
    subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def prepare_smoke_media(ffmpeg_path: Path, root: Path) -> dict:
    speech_dir = root / "speech"
    product_dir = root / "product"
    symptom_dir = root / "symptom"

    for path in (speech_dir, product_dir, symptom_dir):
        path.mkdir(parents=True, exist_ok=True)

    create_sample_video(ffmpeg_path, speech_dir / "smoke_speech.mp4", "black", 8.0)

    product_colors = ["red", "orange", "yellow", "gold"]
    symptom_colors = ["blue", "purple", "green", "brown"]
    for index, color in enumerate(product_colors, start=1):
        create_sample_video(ffmpeg_path, product_dir / f"product_{index}.mp4", color, 2.6)
    for index, color in enumerate(symptom_colors, start=1):
        create_sample_video(ffmpeg_path, symptom_dir / f"symptom_{index}.mp4", color, 2.6)

    return {
        "speech_dir": str(speech_dir),
        "product_dir": str(product_dir),
        "symptom_dir": str(symptom_dir),
    }


def find_generated_drafts(draft_root: Path) -> list[str]:
    if not draft_root.exists():
        return []
    matches = []
    for child in draft_root.iterdir():
        if child.is_dir():
            if (child / "draft_content.json").exists() and (child / "draft_meta_info.json").exists():
                matches.append(child.name)
    return sorted(matches)


def run_smoke_generation(exe_path: Path, dist_root: Path, ffmpeg_path: Path) -> dict:
    sandbox_root = Path(tempfile.mkdtemp(prefix="yunfeng_release_smoke_"))
    media_root = sandbox_root / "media"
    output_root = sandbox_root / "output"
    draft_root = sandbox_root / "drafts"
    media_info = {}

    try:
        media_info = prepare_smoke_media(ffmpeg_path, media_root)
        env = os.environ.copy()
        env.update(
            {
                "OTC_SPEECH_DIR": media_info["speech_dir"],
                "OTC_PRODUCT_DIR": media_info["product_dir"],
                "OTC_SYMPTOM_DIR": media_info["symptom_dir"],
                "OTC_OUTPUT_DIR": str(output_root),
                "OTC_DRAFT_ROOT": str(draft_root),
                "LLM_API_KEY": "",
                "LLM_BASE_URL": "",
                "LLM_MODEL": "deepseek-v3.2",
            }
        )

        completed = subprocess.run(
            [str(exe_path), "--worker", "--sensitivity", "medium", "--video", "smoke_speech.mp4"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(dist_root),
            env=env,
            timeout=420,
        )

        generated_drafts = find_generated_drafts(draft_root)
        return {
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-2000:],
            "draft_root": str(draft_root),
            "generated_drafts": generated_drafts,
            "sample_media": media_info,
            "ok": completed.returncode == 0 and bool(generated_drafts),
        }
    finally:
        shutil.rmtree(sandbox_root, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="校验打包发布产物")
    parser.add_argument(
        "--dist-root",
        type=str,
        default=str(PROJECT_ROOT / "dist" / "YunFengEditor"),
        help="发布目录路径",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dist_root = Path(args.dist_root).resolve()
    internal_root = dist_root / "_internal"
    exe_path = dist_root / "YunFengEditor.exe"
    report_path = dist_root / "release_verification.json"
    ffmpeg_path = internal_root / "ffmpeg-8.1-essentials_build" / "bin" / "ffmpeg.exe"

    report = {
        "dist_root": str(dist_root),
        "checks": {
            "exe": check_exists(exe_path),
            "ffmpeg": check_exists(ffmpeg_path),
            "ffprobe": check_exists(internal_root / "ffmpeg-8.1-essentials_build" / "bin" / "ffprobe.exe"),
            "skill_wrapper": check_exists(
                internal_root
                / "jianying-editor-skill-main"
                / "jianying-editor-skill-main"
                / "scripts"
                / "jy_wrapper.py"
            ),
            "whisper_base_model": check_exists(internal_root / ".whisper_cache" / "base.pt"),
            "subtitle_panel": check_exists(internal_root / "subtitle_sync_panel.html"),
        },
        "worker_help": None,
        "worker_preflight": None,
        "gui_smoke_test": None,
        "smoke_generation": None,
        "ok": False,
    }

    if not exe_path.exists():
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[失败] 未找到发布程序: {exe_path}")
        return 1

    report["worker_help"] = run_worker_help(exe_path)
    report["worker_preflight"] = run_worker_preflight(exe_path, dist_root)
    report["gui_smoke_test"] = run_gui_smoke_test(exe_path, dist_root)
    report["smoke_generation"] = run_smoke_generation(exe_path, dist_root, ffmpeg_path)

    required_keys = ["exe", "ffmpeg", "ffprobe", "skill_wrapper"]
    required_ok = all(report["checks"][key]["exists"] for key in required_keys)
    report["ok"] = (
        required_ok
        and report["worker_help"]["ok"]
        and report["worker_preflight"]["ok"]
        and report["gui_smoke_test"]["still_running_after_8s"]
        and report["smoke_generation"]["ok"]
    )

    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[报告] 发布校验报告已写入: {report_path}")
    print(f"[结果] {'通过' if report['ok'] else '失败'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
