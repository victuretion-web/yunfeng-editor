import base64
import ctypes
import json
import os
from ctypes import wintypes
from typing import Dict

from app_paths import runtime_path


_STORE_PATH = runtime_path("output", "ui_secure_store.json")
_STORE_SCOPE = "yunfeng_editor_ui"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob_from_bytes(raw: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(raw)
    blob = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _protect_bytes(raw: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("当前仅支持 Windows DPAPI 加密存储")
    if not raw:
        return b""

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    input_blob, input_buffer = _blob_from_bytes(raw)
    output_blob = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "YunFengEditor API Key",
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()

    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(output_blob.pbData)
        del input_buffer


def _unprotect_bytes(raw: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("当前仅支持 Windows DPAPI 解密存储")
    if not raw:
        return b""

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    input_blob, input_buffer = _blob_from_bytes(raw)
    output_blob = _DataBlob()
    description = wintypes.LPWSTR()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()

    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if description:
            kernel32.LocalFree(description)
        if output_blob.pbData:
            kernel32.LocalFree(output_blob.pbData)
        del input_buffer


def _load_store() -> Dict[str, str]:
    if not os.path.exists(_STORE_PATH):
        return {}
    with open(_STORE_PATH, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        return {}
    scoped = payload.get(_STORE_SCOPE, {})
    return dict(scoped) if isinstance(scoped, dict) else {}


def _write_store(values: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(_STORE_PATH), exist_ok=True)
    payload = {_STORE_SCOPE: values}
    with open(_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def save_secret(name: str, value: str) -> None:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("缺少安全存储名称")

    store = _load_store()
    text = str(value or "")
    if not text:
        store.pop(clean_name, None)
        _write_store(store)
        return

    encrypted = _protect_bytes(text.encode("utf-8"))
    store[clean_name] = base64.b64encode(encrypted).decode("ascii")
    _write_store(store)


def load_secret(name: str) -> str:
    clean_name = str(name or "").strip()
    if not clean_name:
        return ""
    store = _load_store()
    payload = str(store.get(clean_name, "") or "").strip()
    if not payload:
        return ""
    encrypted = base64.b64decode(payload.encode("ascii"))
    return _unprotect_bytes(encrypted).decode("utf-8")
