"""Windows DPAPI + Chromium AES-GCM helpers. Never log plaintext secrets."""

from __future__ import annotations

import ctypes
from ctypes import wintypes


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


class _BCRYPT_AUTH_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.ULONG),
        ("dwInfoVersion", wintypes.ULONG),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbNonce", wintypes.ULONG),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbAuthData", wintypes.ULONG),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbTag", wintypes.ULONG),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbMacContext", wintypes.ULONG),
        ("cbAAD", wintypes.ULONG),
        ("cbData", ctypes.c_ulonglong),
        ("dwFlags", wintypes.ULONG),
    ]


def dpapi_protect(data: bytes) -> bytes:
    blob_in = _DATA_BLOB(len(data), ctypes.create_string_buffer(data, len(data)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def dpapi_unprotect(data: bytes) -> bytes:
    blob_in = _DATA_BLOB(len(data), ctypes.create_string_buffer(data, len(data)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _nt_ok(status: int, what: str) -> None:
    if status < 0:
        raise OSError(f"{what} failed NTSTATUS={status:#x}")


def aesgcm_decrypt(key: bytes, nonce: bytes, ciphertext_and_tag: bytes) -> bytes:
    bcrypt = ctypes.windll.bcrypt
    tag = ciphertext_and_tag[-16:]
    ct = ciphertext_and_tag[:-16]
    h_alg = ctypes.c_void_p()
    _nt_ok(bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0), "OpenAlgorithm")
    try:
        mode = "ChainingModeGCM"
        _nt_ok(
            bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, (len(mode) + 1) * 2, 0),
            "SetProperty",
        )
        h_key = ctypes.c_void_p()
        key_buf = (ctypes.c_ubyte * len(key)).from_buffer_copy(key)
        _nt_ok(
            bcrypt.BCryptGenerateSymmetricKey(
                h_alg, ctypes.byref(h_key), None, 0, key_buf, len(key), 0
            ),
            "GenerateKey",
        )
        try:
            nonce_buf = (ctypes.c_ubyte * len(nonce)).from_buffer_copy(nonce)
            tag_buf = (ctypes.c_ubyte * len(tag)).from_buffer_copy(tag)
            info = _BCRYPT_AUTH_INFO()
            info.cbSize = ctypes.sizeof(info)
            info.dwInfoVersion = 1
            info.pbNonce = ctypes.cast(nonce_buf, ctypes.POINTER(ctypes.c_ubyte))
            info.cbNonce = len(nonce)
            info.pbTag = ctypes.cast(tag_buf, ctypes.POINTER(ctypes.c_ubyte))
            info.cbTag = len(tag)
            ct_buf = (ctypes.c_ubyte * len(ct)).from_buffer_copy(ct)
            out = (ctypes.c_ubyte * max(len(ct), 1))()
            cb_result = wintypes.ULONG()
            _nt_ok(
                bcrypt.BCryptDecrypt(
                    h_key,
                    ct_buf,
                    len(ct),
                    ctypes.byref(info),
                    None,
                    0,
                    out,
                    len(ct),
                    ctypes.byref(cb_result),
                    0,
                ),
                "Decrypt",
            )
            return bytes(out[: cb_result.value])
        finally:
            bcrypt.BCryptDestroyKey(h_key)
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


def decrypt_chromium_v10(blob: bytes, aes_key: bytes) -> bytes:
    if blob.startswith(b"v10") or blob.startswith(b"v11"):
        return aesgcm_decrypt(aes_key, blob[3:15], blob[15:])
    if blob.startswith(b"DPAPI"):
        return dpapi_unprotect(blob[5:])
    return dpapi_unprotect(blob)
