import json
import os
import shutil
import time
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple


def get_draft_root() -> str:
    return os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "JianyingPro",
        "User Data",
        "Projects",
        "com.lveditor.draft",
    )


def _read_lock_payload(lock_path: str) -> Tuple[Optional[int], Optional[float]]:
    try:
        with open(lock_path, "r", encoding="ascii", errors="ignore") as f:
            parts = f.read().strip().split()
    except OSError:
        return None, None

    if len(parts) != 2:
        return None, None

    try:
        return int(parts[0]), float(parts[1])
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

    draft_id = str(meta.get("draft_id") or meta.get("id") or content.get("id") or "").strip()
    if not draft_id:
        return None, "missing draft id"

    return {
        "draft_fold_path": draft_dir.replace("\\", "/"),
        "draft_id": draft_id,
        "draft_json_file": content_path.replace("\\", "/"),
        "name": os.path.basename(draft_dir),
    }, None


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

    report = {
        "draft_root": draft_root,
        "restored_from_recycle": [],
        "invalid_drafts": [],
        "registered_drafts": [],
        "written_at": int(time.time()),
    }

    with file_lock(lock_path, timeout=120.0):
        if restore_project_drafts and os.path.isdir(recycle_bin):
            for name in os.listdir(recycle_bin):
                if project_prefixes and not any(name.startswith(prefix) for prefix in project_prefixes):
                    continue
                recycle_path = os.path.join(recycle_bin, name)
                target_path = os.path.join(draft_root, name)
                if not os.path.isdir(recycle_path) or os.path.exists(target_path):
                    continue
                draft_entry, _ = _read_valid_draft(recycle_path)
                if not draft_entry:
                    continue
                shutil.move(recycle_path, target_path)
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
