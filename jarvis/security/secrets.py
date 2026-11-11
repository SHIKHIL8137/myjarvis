"""Encrypted API key & portable DR vault storage (Windows DPAPI at rest + AES-256-GCM export)."""
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_DPAPI_PREFIX = "dpapi:"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_available() -> bool:
    return os.name == "nt" and hasattr(ctypes, "windll") and hasattr(ctypes.windll, "crypt32")


def _dpapi_encrypt(plaintext: str) -> str:
    if not _dpapi_available():
        return plaintext
    raw = plaintext.encode("utf-8")
    buf = ctypes.create_string_buffer(raw, len(raw))
    in_blob = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DATA_BLOB()
    # CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(in_blob), "JarvisSecret", None, None, None, 0x1, ctypes.byref(out_blob))
    if not ok:
        return plaintext
    try:
        cipher = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return _DPAPI_PREFIX + base64.b64encode(cipher).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _dpapi_decrypt(stored: str) -> str:
    if not stored.startswith(_DPAPI_PREFIX):
        return stored
    if not _dpapi_available():
        return ""
    cipher = base64.b64decode(stored[len(_DPAPI_PREFIX):].encode("ascii"))
    buf = ctypes.create_string_buffer(cipher, len(cipher))
    in_blob = _DATA_BLOB(len(cipher), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0x1, ctypes.byref(out_blob))
    if not ok:
        return ""
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8", errors="replace")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def load_secrets_map(secrets_path: Path) -> dict[str, str]:
    """Read provider->plaintext map, transparently decrypting DPAPI entries and legacy plaintext entries."""
    if not secrets_path.exists():
        return {}
    try:
        raw = json.loads(secrets_path.read_text(encoding="utf-8"))
        out: dict[str, str] = {}
        for k, v in (raw.items() if isinstance(raw, dict) else []):
            if isinstance(v, str) and v.strip():
                dec = _dpapi_decrypt(v.strip())
                if dec:
                    out[str(k).lower()] = dec
        return out
    except Exception:
        return {}


def save_secrets_map(secrets_path: Path, data: dict[str, str], *, encrypt_dpapi: bool = True) -> None:
    """Persist secrets atomically with DPAPI encryption on Windows."""
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    stored: dict[str, str] = {}
    for k, v in data.items():
        if isinstance(v, str) and v.strip():
            stored[str(k).lower()] = _dpapi_encrypt(v.strip()) if encrypt_dpapi else v.strip()
    tmp = secrets_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(stored, indent=2), encoding="utf-8")
    os.replace(tmp, secrets_path)


def export_encrypted_vault(secrets: dict[str, str], passphrase: str) -> bytes:
    """Create a portable, machine-independent AES-256-GCM PBKDF2 encrypted vault blob for 3-2-1 backups."""
    if not passphrase or len(passphrase) < 8:
        raise ValueError("Backup passphrase must be at least 8 characters")
    salt = os.urandom(16)
    nonce = os.urandom(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    key = kdf.derive(passphrase.encode("utf-8"))
    payload = json.dumps(secrets, ensure_ascii=False).encode("utf-8")
    ct = AESGCM(key).encrypt(nonce, payload, b"jarvis-secrets-vault-v1")
    envelope = {
        "format": "jarvis-vault-v1",
        "kdf": "pbkdf2-sha256-200k",
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ct).decode("ascii"),
    }
    return json.dumps(envelope, indent=2).encode("utf-8")


def import_encrypted_vault(blob: bytes, passphrase: str) -> dict[str, str]:
    """Decrypt a portable DR vault back to provider->secret map."""
    env = json.loads(blob.decode("utf-8"))
    if env.get("format") != "jarvis-vault-v1":
        raise ValueError("Unsupported encrypted vault format")
    salt = base64.b64decode(env["salt"])
    nonce = base64.b64decode(env["nonce"])
    ct = base64.b64decode(env["ciphertext"])
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    key = kdf.derive(passphrase.encode("utf-8"))
    pt = AESGCM(key).decrypt(nonce, ct, b"jarvis-secrets-vault-v1")
    data = json.loads(pt.decode("utf-8"))
    return {str(k).lower(): str(v) for k, v in data.items() if isinstance(v, str)}

