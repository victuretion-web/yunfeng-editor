import os
import subprocess
from typing import Any, Dict


def _hidden_process_options() -> Dict[str, Any]:
    if os.name != "nt":
        return {}

    options: Dict[str, Any] = {}
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if create_no_window:
        options["creationflags"] = create_no_window

    startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_factory is not None:
        startupinfo = startupinfo_factory()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = 0
        options["startupinfo"] = startupinfo

    return options


def apply_hidden_process_options(kwargs: Dict[str, Any] | None = None) -> Dict[str, Any]:
    merged = dict(kwargs or {})
    hidden_options = _hidden_process_options()

    if "creationflags" in hidden_options:
        merged["creationflags"] = int(merged.get("creationflags", 0)) | int(hidden_options["creationflags"])

    if "startupinfo" in hidden_options and "startupinfo" not in merged:
        merged["startupinfo"] = hidden_options["startupinfo"]

    return merged


def run_hidden(command, **kwargs):
    return subprocess.run(command, **apply_hidden_process_options(kwargs))


def popen_hidden(command, **kwargs):
    return subprocess.Popen(command, **apply_hidden_process_options(kwargs))
