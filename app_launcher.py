import os
import subprocess
import sys

from app_paths import configure_current_process


def _is_worker_mode() -> bool:
    return len(sys.argv) > 1 and sys.argv[1] == "--worker"


def _find_pythonw_executable() -> str:
    executable = os.path.abspath(sys.executable or "")
    base_name = os.path.basename(executable).lower()
    if base_name == "pythonw.exe":
        return executable
    if not executable:
        return ""
    candidate = os.path.join(os.path.dirname(executable), "pythonw.exe")
    return candidate if os.path.exists(candidate) else ""


def _should_relaunch_ui_without_console() -> bool:
    if os.name != "nt":
        return False
    if getattr(sys, "frozen", False):
        return False
    if _is_worker_mode():
        return False
    if os.environ.get("OTC_UI_NO_CONSOLE_RELAUNCHED") == "1":
        return False
    return bool(_find_pythonw_executable())


def _relaunch_ui_without_console() -> int:
    pythonw_exe = _find_pythonw_executable()
    if not pythonw_exe:
        return 0

    env = os.environ.copy()
    env["OTC_UI_NO_CONSOLE_RELAUNCHED"] = "1"
    creationflags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        creationflags |= subprocess.DETACHED_PROCESS
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP

    subprocess.Popen(
        [pythonw_exe, *sys.argv],
        env=env,
        cwd=os.getcwd(),
        creationflags=creationflags,
        close_fds=True,
    )
    return 0


def main() -> int:
    configure_current_process()

    if _should_relaunch_ui_without_console():
        return _relaunch_ui_without_console()

    if _is_worker_mode():
        from otc_promo_workflow import main as worker_main

        sys.argv = [sys.argv[0], *sys.argv[2:]]
        return int(worker_main())

    from ui_main import YunFengEditorUI

    app = YunFengEditorUI()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
