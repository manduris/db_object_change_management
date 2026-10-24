"""Encrypt secrets with the current Windows user account."""

from __future__ import annotations

import base64
import ctypes
from ctypes import POINTER, Structure, WinDLL, c_char, c_void_p, cast, create_string_buffer, string_at
from ctypes import wintypes

_CRYPTPROTECT_UI_FORBIDDEN = 0x01
_PREFIX = "dpapi:v1:"

crypt32 = WinDLL("crypt32", use_last_error=True)
kernel32 = WinDLL("kernel32", use_last_error=True)


class DATA_BLOB(Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", POINTER(c_char))]


crypt32.CryptProtectData.argtypes = [
    POINTER(DATA_BLOB),
    wintypes.LPCWSTR,
    POINTER(DATA_BLOB),
    c_void_p,
    c_void_p,
    wintypes.DWORD,
    POINTER(DATA_BLOB),
]
crypt32.CryptProtectData.restype = wintypes.BOOL
crypt32.CryptUnprotectData.argtypes = [
    POINTER(DATA_BLOB),
    POINTER(wintypes.LPWSTR),
    POINTER(DATA_BLOB),
    c_void_p,
    c_void_p,
    wintypes.DWORD,
    POINTER(DATA_BLOB),
]
crypt32.CryptUnprotectData.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = [c_void_p]
kernel32.LocalFree.restype = c_void_p


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = create_string_buffer(data, len(data))
    blob = DATA_BLOB(len(data), cast(buffer, POINTER(c_char)))
    return blob, buffer


def _copy_and_free(blob: DATA_BLOB) -> bytes:
    try:
        return string_at(blob.pbData, blob.cbData)
    finally:
        kernel32.LocalFree(cast(blob.pbData, c_void_p))


def protect_text(value: str) -> str:
    raw = value.encode("utf-8")
    blob_in, _buffer = _blob(raw)
    blob_out = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    encrypted = _copy_and_free(blob_out)
    return _PREFIX + base64.b64encode(encrypted).decode("ascii")


def unprotect_text(value: str) -> str:
    if not value.startswith(_PREFIX):
        raise ValueError("암호화된 비밀번호 형식이 아닙니다.")
    encrypted = base64.b64decode(value[len(_PREFIX) :])
    blob_in, _buffer = _blob(encrypted)
    blob_out = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return _copy_and_free(blob_out).decode("utf-8")
