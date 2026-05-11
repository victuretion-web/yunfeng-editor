import json
import math
import os
import shutil
import time
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

from app_paths import runtime_path

MANAGED_DRAFT_PREFIXES: Tuple[str, ...] = ("OTC推广_", "OTCPreview")


def get_official_draft_root() -> str:
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_appdata:
        return ""
    return os.path.join(
        local_appdata,
        "JianyingPro",
        "User Data",
        "Projects",
        "com.lveditor.draft",
    )


def get_portable_draft_root() -> str:
    return runtime_path("_sandbox_drafts", "JianyingPro", "User Data", "Projects", "com.lveditor.draft")


def is_portable_draft_root(path: Optional[str]) -> bool:
    if not path:
        return False
    try:
        return os.path.abspath(path) == os.path.abspath(get_portable_draft_root())
    except Exception:
        return False


def _ensure_writable_directory(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe_path = os.path.join(path, ".write_probe.tmp")
        with open(probe_path, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe_path)
        return True
    except OSError:
        return False


def get_draft_root() -> str:
    override_root = os.environ.get("OTC_DRAFT_ROOT", "").strip()
    if override_root:
        return override_root

    official_root = get_official_draft_root()
    if official_root and _ensure_writable_directory(official_root):
        return official_root

    portable_root = get_portable_draft_root()
    os.makedirs(portable_root, exist_ok=True)
    return portable_root


def _read_lock_payload(lock_path: str) -> Tuple[Optional[int], Optional[float]]:
    try:
        with open(lock_path, "r", encoding="ascii", errors="ignore") as f:
            parts = f.read().strip().split()
    except OSError:
        return None, None

    if len(parts) != 2:
        return None, None

    try:
        pid = int(parts[0])
        created_at = float(parts[1])
        if not math.isfinite(created_at) or pid <= 0:
            return None, None
        return pid, created_at
    except ValueError:
        return None, None


def _pid_is_running(pid: Optional[int]) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextmanager
def file_lock(lock_path: str, timeout: float = 60.0, poll_interval: float = 0.2):
    start_time = time.time()
    lock_fd = None
    while True:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(lock_fd, f"{os.getpid()} {time.time()}".encode("ascii", errors="ignore"))
            break
        except FileExistsError:
            lock_pid, lock_created_at = _read_lock_payload(lock_path)
            lock_age = time.time() - lock_created_at if lock_created_at is not None else None
            stale_lock = (
                lock_created_at is None
                or (lock_age is not None and lock_age > timeout)
                or not _pid_is_running(lock_pid)
            )
            if stale_lock:
                try:
                    os.remove(lock_path)
                    continue
                except FileNotFoundError:
                    continue
                except OSError:
                    pass
            if time.time() - start_time >= timeout:
                raise TimeoutError(f"获取锁超时: {lock_path}")
            time.sleep(poll_interval)

    try:
        yield
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass


def _load_json(path: str) -> Tuple[Optional[Dict], Optional[str]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as exc:
        return None, str(exc)


def _atomic_write_json(path: str, payload: Dict):
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def _is_hidden_name(name: str) -> bool:
    return name.startswith(".")


def _is_recycle_archive_name(name: str) -> bool:
    return name.startswith(".recycle_archive_")


def _normalize_project_prefixes(project_prefixes: Tuple[str, ...]) -> Tuple[str, ...]:
    merged: List[str] = []
    for prefix in (*MANAGED_DRAFT_PREFIXES, *(project_prefixes or ())):
        normalized = str(prefix or "").strip()
        if normalized and normalized not in merged:
            merged.append(normalized)
    return tuple(merged)


def _matches_project_prefix(name: str, project_prefixes: Tuple[str, ...]) -> bool:
    if not project_prefixes:
        return True
    return any(name.startswith(prefix) for prefix in project_prefixes)


def _first_not_none(*values):
    for v in values:
        if v is not None:
            return str(v)
    return ""


def _read_valid_draft(draft_dir: str) -> Tuple[Optional[Dict], Optional[str]]:
    content_path = os.path.join(draft_dir, "draft_content.json")
    meta_path = os.path.join(draft_dir, "draft_meta_info.json")
    if not os.path.exists(content_path) or not os.path.exists(meta_path):
        return None, "missing core files"

    content, content_err = _load_json(content_path)
    meta, meta_err = _load_json(meta_path)
    if content is None:
        return None, f"invalid draft_content.json: {content_err}"
    if meta is None:
        return None, f"invalid draft_meta_info.json: {meta_err}"

    draft_id = _first_not_none(
        meta.get("draft_id"), meta.get("id"), content.get("id"),
    ).strip()
    if not draft_id:
        return None, "missing draft id"

    return {
        "draft_fold_path": draft_dir.replace("\\", "/"),
        "draft_id": draft_id,
        "draft_json_file": content_path.replace("\\", "/"),
        "name": os.path.basename(draft_dir),
    }, None


def _extract_indexed_draft_names(root_meta: Optional[Dict]) -> List[str]:
    if not isinstance(root_meta, dict):
        return []

    indexed_names: List[str] = []
    for item in root_meta.get("all_draft_store", []) or []:
        if not isinstance(item, dict):
            continue
        draft_fold_path = str(item.get("draft_fold_path") or "").strip()
        if not draft_fold_path:
            continue
        name = os.path.basename(draft_fold_path.replace("/", os.sep))
        if name:
            indexed_names.append(name)
    return indexed_names


def _iter_recycle_sources(draft_root: str) -> List[Tuple[str, str]]:
    sources: List[Tuple[str, str]] = []
    recycle_bin = os.path.join(draft_root, ".recycle_bin")
    if os.path.isdir(recycle_bin):
        sources.append(("recycle_bin", recycle_bin))

    archive_dirs: List[str] = []
    for name in os.listdir(draft_root):
        if not _is_recycle_archive_name(name):
            continue
        archive_path = os.path.join(draft_root, name)
        if os.path.isdir(archive_path):
            archive_dirs.append(archive_path)

    # Newer archive directories use larger timestamp suffixes.
    for archive_path in sorted(archive_dirs, reverse=True):
        sources.append(("recycle_archive", archive_path))
    return sources


def _can_restore_to_target(target_path: str) -> bool:
    if not os.path.exists(target_path):
        return True

    if not os.path.isdir(target_path):
        return False

    draft_entry, _ = _read_valid_draft(target_path)
    if draft_entry:
        return False

    shutil.rmtree(target_path, ignore_errors=False)
    return True


def reconcile_root_meta(
    draft_root: Optional[str] = None,
    restore_project_drafts: bool = False,
    project_prefixes: Tuple[str, ...] = ("OTC推广_",),
    report_path: Optional[str] = None,
    lock_path: Optional[str] = None,
) -> Dict:
    draft_root = draft_root or get_draft_root()
    os.makedirs(draft_root, exist_ok=True)
    recycle_bin = os.path.join(draft_root, ".recycle_bin")
    root_meta_path = os.path.join(draft_root, "root_meta_info.json")
    lock_path = lock_path or os.path.join(draft_root, ".root_meta_info.lock")
    project_prefixes = _normalize_project_prefixes(project_prefixes)

    report = {
        "draft_root": draft_root,
        "restored_from_recycle": [],
        "restored_from_archive": [],
        "invalid_drafts": [],
        "registered_drafts": [],
        "written_at": int(time.time()),
    }

    with file_lock(lock_path, timeout=120.0):
        recycle_sources = _iter_recycle_sources(draft_root)
        root_meta, _ = _load_json(root_meta_path)
        indexed_names = set(_extract_indexed_draft_names(root_meta))

        if restore_project_drafts:
            for source_kind, source_root in recycle_sources:
                for name in os.listdir(source_root):
                    if not _matches_project_prefix(name, project_prefixes):
                        continue
                    source_path = os.path.join(source_root, name)
                    if not os.path.isdir(source_path):
                        continue
                    draft_entry, _ = _read_valid_draft(source_path)
                    if not draft_entry:
                        continue

                    target_path = os.path.join(draft_root, name)
                    if not _can_restore_to_target(target_path):
                        continue

                    shutil.move(source_path, target_path)
                    if source_kind == "recycle_archive":
                        report["restored_from_archive"].append(name)
                    else:
                        report["restored_from_recycle"].append(name)

        all_draft_store: List[Dict] = []
        for name in sorted(os.listdir(draft_root)):
            if _is_hidden_name(name) or name == ".recycle_bin":
                continue
            draft_dir = os.path.join(draft_root, name)
            if not os.path.isdir(draft_dir):
                continue
            draft_entry, error = _read_valid_draft(draft_dir)
            if draft_entry:
                report["registered_drafts"].append(name)
                all_draft_store.append({
                    "draft_fold_path": draft_entry["draft_fold_path"],
                    "draft_id": draft_entry["draft_id"],
                    "draft_json_file": draft_entry["draft_json_file"],
                })
            else:
                report["invalid_drafts"].append({"name": name, "reason": error})

        root_meta = {
            "all_draft_store": all_draft_store,
            "draft_ids": len(all_draft_store),
            "root_path": draft_root.replace("\\", "/"),
        }
        _atomic_write_json(root_meta_path, root_meta)

    if report_path:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        _atomic_write_json(report_path, report)

    return report
