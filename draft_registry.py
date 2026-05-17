import json
import math
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

from app_paths import runtime_path

MANAGED_DRAFT_PREFIXES: Tuple[str, ...] = ("OTC推广_", "OTCPreview")
DRAFT_MODE_AUTO = "auto"
DRAFT_MODE_SYSTEM = "system"
DRAFT_MODE_PORTABLE = "portable"
VALID_DRAFT_MODES = {DRAFT_MODE_AUTO, DRAFT_MODE_SYSTEM, DRAFT_MODE_PORTABLE}


def get_official_draft_root() -> str:
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_appdata:
        user_profile = os.environ.get("USERPROFILE", "").strip()
        home_drive = os.environ.get("HOMEDRIVE", "").strip()
        home_path = os.environ.get("HOMEPATH", "").strip()
        fallback_home = ""
        if user_profile:
            fallback_home = user_profile
        elif home_drive and home_path:
            fallback_home = home_drive + home_path
        elif os.path.expanduser("~"):
            fallback_home = os.path.expanduser("~")

        if fallback_home:
            local_appdata = os.path.join(fallback_home, "AppData", "Local")
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
    except OSError:
        return False


def _ensure_writable_directory(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe_path = os.path.join(path, "write_probe.tmp")
        with open(probe_path, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe_path)
        return True
    except OSError:
        return False


def normalize_draft_mode(mode: Optional[str] = None) -> str:
    normalized = str(mode or os.environ.get("OTC_DRAFT_MODE", DRAFT_MODE_AUTO)).strip().lower()
    if normalized not in VALID_DRAFT_MODES:
        return DRAFT_MODE_AUTO
    return normalized


def get_draft_root_info(mode: Optional[str] = None) -> Dict[str, object]:
    requested_mode = normalize_draft_mode(mode)
    official_root = get_official_draft_root()
    portable_root = get_portable_draft_root()
    official_writable = bool(official_root) and _ensure_writable_directory(official_root)
    portable_writable = _ensure_writable_directory(portable_root)

    resolved_mode = requested_mode
    resolved_root = ""
    fallback_reason = ""

    if requested_mode == DRAFT_MODE_PORTABLE:
        resolved_mode = DRAFT_MODE_PORTABLE
        resolved_root = portable_root
    else:
        # In auto/system mode, always prefer the system JianYing root when it can
        # be resolved. Actual writability is validated later by worker preflight.
        # This avoids false portable fallback caused by transient probe failures.
        if official_root:
            resolved_mode = DRAFT_MODE_SYSTEM
            resolved_root = official_root
        elif portable_root:
            resolved_mode = DRAFT_MODE_PORTABLE
            resolved_root = portable_root
            fallback_reason = "未检测到系统剪映草稿目录，已使用便携目录"

    if resolved_root:
        os.makedirs(resolved_root, exist_ok=True)

    return {
        "requested_mode": requested_mode,
        "resolved_mode": resolved_mode,
        "resolved_root": resolved_root,
        "official_root": official_root,
        "portable_root": portable_root,
        "official_writable": official_writable,
        "portable_writable": portable_writable,
        "using_portable_root": resolved_mode == DRAFT_MODE_PORTABLE,
        "fallback_reason": fallback_reason,
    }


def get_draft_root() -> str:
    requested_mode = normalize_draft_mode()
    override_root = os.environ.get("OTC_DRAFT_ROOT", "").strip()
    # Only honor an explicit override when the caller deliberately selected
    # portable mode. This prevents stale inherited environment variables from
    # forcing the worker back into the sandbox draft root during auto/system mode.
    if override_root and requested_mode == DRAFT_MODE_PORTABLE:
        return override_root
    info = get_draft_root_info(requested_mode)
    return str(info.get("resolved_root") or "")


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


def _rewrite_draft_meta_paths(draft_dir: str, draft_name: str, root_dir: str) -> None:
    meta_path = os.path.join(draft_dir, "draft_meta_info.json")
    meta, meta_err = _load_json(meta_path)
    if meta is None:
        raise ValueError(f"invalid draft_meta_info.json: {meta_err}")

    updated = False
    normalized_draft_dir = draft_dir.replace("\\", "/")
    normalized_root_dir = root_dir.replace("\\", "/")

    if str(meta.get("draft_fold_path") or "").strip() != normalized_draft_dir:
        meta["draft_fold_path"] = normalized_draft_dir
        updated = True
    if str(meta.get("draft_root_path") or "").strip() != normalized_root_dir:
        meta["draft_root_path"] = normalized_root_dir
        updated = True
    if str(meta.get("draft_name") or "").strip() != draft_name:
        meta["draft_name"] = draft_name
        updated = True

    if updated:
        _atomic_write_json(meta_path, meta)


def _build_temp_dir_name(prefix: str) -> str:
    safe_prefix = str(prefix or "draft").replace(" ", "_").replace(":", "_")
    return f"{safe_prefix}.sync_tmp_{int(time.time() * 1000)}_{os.getpid()}"


def _build_backup_dir_name(prefix: str) -> str:
    safe_prefix = str(prefix or "draft").replace(" ", "_").replace(":", "_")
    return f"{safe_prefix}.sync_bak_{int(time.time() * 1000)}_{os.getpid()}"


def _remove_tree_if_exists(path: str) -> None:
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=False)


def _get_sync_staging_root(target_root: str) -> str:
    target_root = os.path.abspath(target_root)
    target_drive = os.path.splitdrive(target_root)[0].lower()
    temp_root = os.path.abspath(tempfile.gettempdir())
    temp_drive = os.path.splitdrive(temp_root)[0].lower()
    if target_drive and temp_drive == target_drive:
        staging_root = os.path.join(temp_root, "yunfeng_draft_sync")
        os.makedirs(staging_root, exist_ok=True)
        return staging_root
    return target_root


def _stage_draft_copy(source_path: str, target_root: str, draft_name: str) -> str:
    staging_root = _get_sync_staging_root(target_root)
    staged_path = os.path.join(staging_root, _build_temp_dir_name(draft_name))
    shutil.copytree(source_path, staged_path)
    _rewrite_draft_meta_paths(staged_path, draft_name, target_root)
    return staged_path


def _commit_staged_draft(staged_path: str, target_path: str) -> None:
    backup_path = ""
    try:
        if os.path.exists(target_path):
            backup_path = os.path.join(os.path.dirname(target_path), _build_backup_dir_name(os.path.basename(target_path)))
            os.replace(target_path, backup_path)
        os.replace(staged_path, target_path)
    except Exception:
        if backup_path and os.path.exists(backup_path) and not os.path.exists(target_path):
            os.replace(backup_path, target_path)
        raise
    finally:
        if os.path.exists(staged_path):
            _remove_tree_if_exists(staged_path)
        if backup_path and os.path.exists(backup_path):
            _remove_tree_if_exists(backup_path)


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


def _normalize_root_meta_entry(item: object) -> Optional[Dict[str, str]]:
    if not isinstance(item, dict):
        return None
    draft_fold_path = str(item.get("draft_fold_path") or "").strip()
    draft_id = str(item.get("draft_id") or "").strip()
    draft_json_file = str(item.get("draft_json_file") or "").strip()
    if not draft_fold_path or not draft_id or not draft_json_file:
        return None
    return {
        "draft_fold_path": draft_fold_path.replace("\\", "/"),
        "draft_id": draft_id,
        "draft_json_file": draft_json_file.replace("\\", "/"),
    }


def _merge_latest_root_meta_entries(
    draft_root: str,
    scanned_entries: List[Dict[str, str]],
    latest_root_meta: Optional[Dict],
) -> Tuple[List[Dict[str, str]], List[str]]:
    merged_entries = list(scanned_entries)
    preserved_names: List[str] = []
    seen_names = {
        os.path.basename(str(entry.get("draft_fold_path") or "").replace("/", os.sep))
        for entry in merged_entries
    }
    for item in (latest_root_meta or {}).get("all_draft_store", []) or []:
        normalized = _normalize_root_meta_entry(item)
        if not normalized:
            continue
        draft_name = os.path.basename(normalized["draft_fold_path"].replace("/", os.sep))
        if not draft_name or draft_name in seen_names:
            continue
        draft_dir = os.path.join(draft_root, draft_name)
        if not os.path.isdir(draft_dir):
            continue
        validated_entry, _ = _read_valid_draft(draft_dir)
        if not validated_entry:
            continue
        merged_entries.append({
            "draft_fold_path": validated_entry["draft_fold_path"],
            "draft_id": validated_entry["draft_id"],
            "draft_json_file": validated_entry["draft_json_file"],
        })
        seen_names.add(draft_name)
        preserved_names.append(draft_name)
    return merged_entries, preserved_names


def _build_root_meta_payload(
    draft_root: str,
    all_draft_store: List[Dict[str, str]],
    previous_root_meta: Optional[Dict] = None,
) -> Dict[str, object]:
    payload = dict(previous_root_meta) if isinstance(previous_root_meta, dict) else {}
    payload["all_draft_store"] = all_draft_store
    payload["draft_ids"] = len(all_draft_store)
    payload["root_path"] = draft_root.replace("\\", "/")
    return payload


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


def sync_managed_drafts(
    source_root: str,
    target_root: str,
    project_prefixes: Tuple[str, ...] = ("OTC推广_",),
    include_names: Optional[Tuple[str, ...]] = None,
    remove_stale_managed: bool = False,
    report_path: Optional[str] = None,
    lock_path: Optional[str] = None,
) -> Dict:
    source_root = os.path.abspath(source_root)
    target_root = os.path.abspath(target_root)
    os.makedirs(source_root, exist_ok=True)
    os.makedirs(target_root, exist_ok=True)
    project_prefixes = _normalize_project_prefixes(project_prefixes)
    include_name_set = {
        str(name or "").strip()
        for name in (include_names or ())
        if str(name or "").strip()
    }
    lock_path = lock_path or os.path.join(target_root, ".draft_sync.lock")

    report = {
        "source_root": source_root,
        "target_root": target_root,
        "include_names": sorted(include_name_set),
        "copied": [],
        "replaced_invalid": [],
        "skipped_existing": [],
        "removed_stale": [],
        "invalid_source_drafts": [],
        "registered_drafts": [],
        "written_at": int(time.time()),
    }

    with file_lock(lock_path, timeout=180.0):
        source_names = sorted(os.listdir(source_root))
        if include_name_set:
            source_names = [name for name in source_names if name in include_name_set]

        for name in source_names:
            if _is_hidden_name(name) or not _matches_project_prefix(name, project_prefixes):
                continue
            source_path = os.path.join(source_root, name)
            if not os.path.isdir(source_path):
                continue

            draft_entry, error = _read_valid_draft(source_path)
            if not draft_entry:
                report["invalid_source_drafts"].append({"name": name, "reason": error})
                continue

            target_path = os.path.join(target_root, name)
            if os.path.exists(target_path):
                if not os.path.isdir(target_path):
                    report["skipped_existing"].append(name)
                    continue
                target_entry, _ = _read_valid_draft(target_path)
                if target_entry:
                    _rewrite_draft_meta_paths(target_path, name, target_root)
                    report["skipped_existing"].append(name)
                    continue
                staged_path = _stage_draft_copy(source_path, target_root, name)
                _commit_staged_draft(staged_path, target_path)
                report["replaced_invalid"].append(name)
            else:
                staged_path = _stage_draft_copy(source_path, target_root, name)
                _commit_staged_draft(staged_path, target_path)
                report["copied"].append(name)

        if remove_stale_managed and include_name_set:
            for name in sorted(os.listdir(target_root)):
                if _is_hidden_name(name) or not _matches_project_prefix(name, project_prefixes):
                    continue
                if name in include_name_set:
                    continue
                target_path = os.path.join(target_root, name)
                if not os.path.isdir(target_path):
                    continue
                shutil.rmtree(target_path, ignore_errors=False)
                report["removed_stale"].append(name)

        reconcile_report = reconcile_root_meta(
            draft_root=target_root,
            restore_project_drafts=False,
            project_prefixes=project_prefixes,
            include_names=tuple(sorted(include_name_set)) if include_name_set else None,
            remove_stale_managed=remove_stale_managed and bool(include_name_set),
            report_path=None,
            lock_path=os.path.join(target_root, ".root_meta_info.lock"),
        )
        report["registered_drafts"] = list(reconcile_report.get("registered_drafts", []))

    if report_path:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        _atomic_write_json(report_path, report)

    return report


def reconcile_root_meta(
    draft_root: Optional[str] = None,
    restore_project_drafts: bool = False,
    project_prefixes: Tuple[str, ...] = ("OTC推广_",),
    include_names: Optional[Tuple[str, ...]] = None,
    remove_stale_managed: bool = False,
    remove_stale_from_recycle: bool = False,
    report_path: Optional[str] = None,
    lock_path: Optional[str] = None,
) -> Dict:
    draft_root = draft_root or get_draft_root()
    os.makedirs(draft_root, exist_ok=True)
    recycle_bin = os.path.join(draft_root, ".recycle_bin")
    root_meta_path = os.path.join(draft_root, "root_meta_info.json")
    lock_path = lock_path or os.path.join(draft_root, ".root_meta_info.lock")
    project_prefixes = _normalize_project_prefixes(project_prefixes)
    include_name_set = {
        str(name or "").strip()
        for name in (include_names or ())
        if str(name or "").strip()
    }

    report = {
        "draft_root": draft_root,
        "include_names": sorted(include_name_set),
        "restored_from_recycle": [],
        "restored_from_archive": [],
        "removed_stale_managed": [],
        "removed_stale_recycle": [],
        "invalid_drafts": [],
        "registered_drafts": [],
        "preserved_external_drafts": [],
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
                    if include_name_set and name not in include_name_set:
                        if remove_stale_from_recycle:
                            shutil.rmtree(source_path, ignore_errors=False)
                            report["removed_stale_recycle"].append({
                                "source": source_kind,
                                "name": name,
                            })
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

        if include_name_set and remove_stale_from_recycle:
            for source_kind, source_root in recycle_sources:
                for name in os.listdir(source_root):
                    if not _matches_project_prefix(name, project_prefixes):
                        continue
                    if name in include_name_set:
                        continue
                    source_path = os.path.join(source_root, name)
                    if not os.path.isdir(source_path):
                        continue
                    shutil.rmtree(source_path, ignore_errors=False)
                    report["removed_stale_recycle"].append({
                        "source": source_kind,
                        "name": name,
                    })

        all_draft_store: List[Dict] = []
        for name in sorted(os.listdir(draft_root)):
            if _is_hidden_name(name) or name == ".recycle_bin":
                continue
            draft_dir = os.path.join(draft_root, name)
            if not os.path.isdir(draft_dir):
                continue
            is_managed_draft = _matches_project_prefix(name, project_prefixes)
            if include_name_set and is_managed_draft and name not in include_name_set:
                if remove_stale_managed:
                    shutil.rmtree(draft_dir, ignore_errors=False)
                    report["removed_stale_managed"].append(name)
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

        latest_root_meta, _ = _load_json(root_meta_path)
        all_draft_store, preserved_external = _merge_latest_root_meta_entries(
            draft_root,
            all_draft_store,
            latest_root_meta,
        )
        for name in preserved_external:
            if name not in report["registered_drafts"]:
                report["registered_drafts"].append(name)
        report["preserved_external_drafts"] = preserved_external
        root_meta = _build_root_meta_payload(draft_root, all_draft_store, latest_root_meta)
        _atomic_write_json(root_meta_path, root_meta)

    if report_path:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        _atomic_write_json(report_path, report)

    return report
