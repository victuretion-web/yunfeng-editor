import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
import traceback
from pathlib import Path

from subprocess_windows import popen_hidden


PROJECT_ROOT = Path(__file__).resolve().parent
BLOCKING_SMOKE_MARKERS = (
    "traceback",
    "[error",
    " error:",
    "fatal error",
    "unhandled exception",
)
REQUIRED_CHECK_LABELS = {
    "exe": "主程序存在",
    "base_library": "base_library.zip 存在",
    "ffmpeg": "ffmpeg 存在",
    "ffprobe": "ffprobe 存在",
    "skill_wrapper": "剪映 skill wrapper 存在",
}
STAGE_LABELS = {
    "worker_help": "worker --help",
    "worker_preflight": "worker --preflight",
    "system_draft_resolution": "草稿目录解析",
    "gui_smoke_test": "GUI 烟测",
    "smoke_generation": "worker 生成烟测",
}


def _safe_run(stage, func, *args):
    try:
        return func(*args)
    except Exception:
        return {"error": traceback.format_exc(), "stage": stage, "ok": False}


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
    process = popen_hidden(
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


def _contains_blocking_smoke_issue(stdout_text: str, stderr_text: str) -> tuple[list[str], list[str]]:
    blocking_issues: list[str] = []
    warnings: list[str] = []
    stdout_text = str(stdout_text or "")
    stderr_text = str(stderr_text or "")
    combined = "\n".join(part for part in (stdout_text, stderr_text) if part)
    lowered = combined.lower()

    if stderr_text.strip():
        warnings.append("worker 烟测产生了 stderr 输出，请复核是否存在非致命环境告警。")
    if "traceback" in lowered:
        blocking_issues.append("worker 烟测输出中包含 Traceback。")
    if "[error" in lowered or " error:" in lowered:
        blocking_issues.append("worker 烟测输出中包含 ERROR 级别异常。")
    if any(marker in lowered for marker in BLOCKING_SMOKE_MARKERS[3:]):
        blocking_issues.append("worker 烟测输出中包含阻断级异常标记。")
    if "[!] 中插占比未达标" in stdout_text:
        warnings.append("最小样本烟测的中插占比未达标，请复核素材池或时间规划策略。")
    if "素材不足 20 条" in stdout_text:
        warnings.append("最小样本烟测提示素材池不足 20 条，该结果仅代表当前样本较弱。")
    return blocking_issues, warnings


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
                "LLM_MODEL": "deepseek-ai/DeepSeek-V4-Flash",
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
        blocking_issues, warnings = _contains_blocking_smoke_issue(completed.stdout, completed.stderr)
        return {
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-2000:],
            "draft_root": str(draft_root),
            "generated_drafts": generated_drafts,
            "sample_media": media_info,
            "blocking_issues": blocking_issues,
            "warnings": warnings,
            "ok": completed.returncode == 0 and bool(generated_drafts) and not blocking_issues,
        }
    finally:
        shutil.rmtree(sandbox_root, ignore_errors=True)


def verify_system_draft_resolution(exe_path: Path, dist_root: Path) -> dict:
    env = os.environ.copy()
    completed = subprocess.run(
        [str(exe_path), "--worker", "--preflight"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(dist_root),
        env=env,
        timeout=120,
    )
    payload = {}
    try:
        payload = json.loads(completed.stdout.strip()) if completed.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    checks = payload.get("checks", {}) if isinstance(payload, dict) else {}
    resolved_root = str(checks.get("draft_root", "")).strip()
    resolved_mode = "portable" if checks.get("using_portable_draft_root") else "system"
    using_portable = bool(checks.get("using_portable_draft_root"))
    reported_official_root = str(checks.get("official_draft_root", "")).strip()
    expected_root = str(
        Path.home() / "AppData" / "Local" / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft"
    )
    resolved_root_norm = os.path.normcase(os.path.abspath(resolved_root)) if resolved_root else ""
    expected_root_norm = os.path.normcase(os.path.abspath(reported_official_root or expected_root))
    warnings = []
    if using_portable and resolved_root:
        warnings.append(f"当前回退到便携草稿目录: {resolved_root}")
    return {
        "returncode": completed.returncode,
        "stdout_head": completed.stdout[:3000],
        "stderr_head": completed.stderr[:2000],
        "payload": payload,
        "expected_root": expected_root,
        "reported_official_root": reported_official_root,
        "resolved_root": resolved_root,
        "resolved_mode": resolved_mode,
        "warnings": warnings,
        "ok": completed.returncode in (0, 1)
        and bool(resolved_root)
        and (
            using_portable
            or resolved_root_norm == expected_root_norm
        ),
    }


def _stage_passed(stage_name: str, stage_result: dict) -> bool:
    if not isinstance(stage_result, dict):
        return False
    if stage_name == "gui_smoke_test":
        return bool(stage_result.get("still_running_after_8s", False))
    return bool(stage_result.get("ok", False))


def build_release_summary(report: dict) -> dict:
    passed_items: list[str] = []
    warning_items: list[str] = []
    blocking_items: list[str] = []

    for check_key, label in REQUIRED_CHECK_LABELS.items():
        check_result = (report.get("checks") or {}).get(check_key) or {}
        if check_result.get("exists", False):
            passed_items.append(label)
        else:
            blocking_items.append(f"{label} 缺失")

    for stage_name, label in STAGE_LABELS.items():
        stage_result = report.get(stage_name) or {}
        if _stage_passed(stage_name, stage_result):
            passed_items.append(label)
        else:
            if stage_result.get("error"):
                blocking_items.append(f"{label} 执行异常")
            elif stage_name == "gui_smoke_test":
                blocking_items.append(f"{label} 未通过，程序未能稳定运行 8 秒")
            elif stage_result.get("blocking_issues"):
                for item in stage_result.get("blocking_issues", []):
                    blocking_items.append(f"{label}: {item}")
            elif stage_name == "worker_preflight":
                payload = stage_result.get("payload") or {}
                for item in payload.get("fatal_errors", []):
                    blocking_items.append(f"{label}: {item}")
            elif stage_name == "system_draft_resolution":
                resolved_root = str(stage_result.get("resolved_root", "")).strip() or "<empty>"
                blocking_items.append(f"{label} 未通过，当前解析结果: {resolved_root}")
            else:
                return_code = stage_result.get("returncode")
                if return_code is None:
                    blocking_items.append(f"{label} 未通过")
                else:
                    blocking_items.append(f"{label} 未通过，返回码: {return_code}")

        for item in stage_result.get("warnings", []) or []:
            warning_items.append(f"{label}: {item}")

    return {
        "passed": passed_items,
        "warnings": warning_items,
        "blocking": blocking_items,
    }


def print_release_summary(report_path: Path, report: dict) -> None:
    summary = build_release_summary(report)
    print(f"[报告] 发布校验报告已写入: {report_path}")
    print(f"[结果] {'通过' if report.get('ok') else '失败'}")

    if summary["passed"]:
        print("[通过项]")
        for item in summary["passed"]:
            print(f"  - {item}")

    if summary["warnings"]:
        print("[警告]")
        for item in summary["warnings"]:
            print(f"  - {item}")

    if summary["blocking"]:
        print("[阻断项]")
        for item in summary["blocking"]:
            print(f"  - {item}")


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
            "base_library": check_exists(internal_root / "base_library.zip"),
            "ffmpeg": check_exists(ffmpeg_path),
            "ffprobe": check_exists(internal_root / "ffmpeg-8.1-essentials_build" / "bin" / "ffprobe.exe"),
            "skill_wrapper": check_exists(
                internal_root
                / "jianying-editor-skill-main"
                / "jianying-editor-skill-main"
                / "scripts"
                / "jy_wrapper.py"
            ),


        },
        "worker_help": None,
        "worker_preflight": None,
        "system_draft_resolution": None,
        "gui_smoke_test": None,
        "smoke_generation": None,
        "ok": False,
    }

    if not exe_path.exists():
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[失败] 未找到发布程序: {exe_path}")
        return 1

    report["worker_help"] = _safe_run("worker_help", run_worker_help, exe_path)
    report["worker_preflight"] = _safe_run("worker_preflight", run_worker_preflight, exe_path, dist_root)
    report["system_draft_resolution"] = _safe_run("system_draft_resolution", verify_system_draft_resolution, exe_path, dist_root)
    report["gui_smoke_test"] = _safe_run("gui_smoke_test", run_gui_smoke_test, exe_path, dist_root)
    report["smoke_generation"] = _safe_run("smoke_generation", run_smoke_generation, exe_path, dist_root, ffmpeg_path)

    required_keys = ["exe", "base_library", "ffmpeg", "ffprobe", "skill_wrapper"]
    required_ok = all(report["checks"][key].get("exists", False) for key in required_keys)
    report["ok"] = (
        required_ok
        and (report.get("worker_help") or {}).get("ok", False)
        and (report.get("worker_preflight") or {}).get("ok", False)
        and (report.get("system_draft_resolution") or {}).get("ok", False)
        and (report.get("gui_smoke_test") or {}).get("still_running_after_8s", False)
        and (report.get("smoke_generation") or {}).get("ok", False)
    )

    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print_release_summary(report_path, report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
