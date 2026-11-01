"""Exness Trader API request signing (EXN-* headers, Ed25519, sign version 1).

Implements the documented protocol exactly:
  EXN-DATA  = base64url(JSON payload) without '=' padding
  EXN-SIGN  = base64url(Ed25519.sign(decoded EXN-DATA bytes)) without '=' padding
  payload   = {api_key, idempotency_key, timestamp(ms), sign_version=1, method, path, body_hash}
  body_hash = base64url(SHA-256(exact body bytes)) without padding
  GET: EXN-IDEMPOTENCY-KEY header present but EMPTY; mutating: non-empty (5–128 chars of A-Za-z0-9._-~)
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from typing import Dict, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

EMPTY_BODY_HASH = "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"   # documented SHA-256('') value


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def body_hash(body: bytes) -> str:
    return b64url(hashlib.sha256(body).digest())


def load_private_key(text: str) -> Ed25519PrivateKey:
    """Accepts the two formats Exness documents:
    - 128 hex chars (Exness-generated): 32-byte seed + 32-byte public key
    - PKCS#8 PEM (your own `openssl genpkey -algorithm ED25519`)
    Also tolerates 64-hex (seed only) and raw base64 seeds.
    """
    t = text.strip()
    if "BEGIN" in t:
        return serialization.load_pem_private_key(t.encode(), password=None)
    h = t.replace(" ", "").replace("\n", "")
    if re.fullmatch(r"[0-9a-fA-F]{128}", h):
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(h)[:32])
    if re.fullmatch(r"[0-9a-fA-F]{64}", h):
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(h))
    try:
        raw = base64.b64decode(h + "=" * (-len(h) % 4))
        if len(raw) in (32, 64):
            return Ed25519PrivateKey.from_private_bytes(raw[:32])
    except Exception:
        pass
    raise ValueError("Private key must be 128-hex (Exness) or PKCS#8 PEM (openssl)")


def public_key_registration_b64(key: Ed25519PrivateKey) -> str:
    """Standard padded Base64 of the SPKI DER public key — the format Key Management accepts."""
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(der).decode()


def new_idempotency_key() -> str:
    return "iitian-" + secrets.token_urlsafe(18).replace("=", "")   # 25 chars, allowed alphabet


class ExnessSigner:
    def __init__(self, api_key: str, private_key_text: str):
        self.api_key = api_key.strip()
        self.key = load_private_key(private_key_text)

    def headers(self, method: str, path_with_query: str, body: Optional[bytes] = None,
                idempotency_key: Optional[str] = None, timestamp_ms: Optional[int] = None) -> Dict[str, str]:
        method = method.upper()
        ts = timestamp_ms or int(time.time() * 1000)
        if method == "GET":
            idem = ""                                    # must be present and empty
        else:
            idem = idempotency_key or new_idempotency_key()
        payload = {
            "api_key": self.api_key,
            "idempotency_key": idem,
            "timestamp": ts,
            "sign_version": 1,
            "method": method,
            "path": path_with_query,
            "body_hash": body_hash(body) if body else EMPTY_BODY_HASH,
        }
        data = json.dumps(payload, separators=(",", ":")).encode()
        return {
            "EXN-API-KEY": self.api_key,
            "EXN-IDEMPOTENCY-KEY": idem,
            "EXN-TIMESTAMP": str(ts),
            "EXN-SIGN-VERSION": "1",
            "EXN-DATA": b64url(data),
            "EXN-SIGN": b64url(self.key.sign(data)),
        }
