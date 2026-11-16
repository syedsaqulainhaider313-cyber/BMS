from __future__ import annotations

import base64
import hashlib
import hmac
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.settings import settings

SNS_HOST_REGEX = re.compile(r"^sns\.[a-z0-9\-]+\.amazonaws\.com(\.cn)?$", re.IGNORECASE)
MAX_CERT_CACHE_SIZE = 100
_CERT_CACHE: dict[str, Any] = {}

# Check if cryptography library is available
try:
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False


def validate_signing_cert_url(url: str) -> None:
    """Strictly validates SigningCertURL to prevent SSRF and certificate spoofing attacks."""
    if not url:
        raise ValueError("Missing SigningCertURL")
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"Insecure certificate scheme '{parsed.scheme}'; https required")
    if parsed.port not in (None, 443):
        raise ValueError(f"Invalid certificate port '{parsed.port}'; port 443 required")
    host = parsed.hostname or ""
    if not SNS_HOST_REGEX.match(host):
        raise ValueError(f"Untrusted certificate host '{host}'; must match amazonaws.com SNS domain")
    if not parsed.path.endswith(".pem") or ".." in parsed.path:
        raise ValueError(f"Invalid certificate path '{parsed.path}'; must end with .pem and contain no traversal")


def validate_subscribe_url(url: str) -> None:
    """Strictly validates SubscribeURL to prevent intranet SSRF attacks."""
    if not url:
        raise ValueError("Missing SubscribeURL")
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"Insecure subscribe scheme '{parsed.scheme}'; https required")
    if parsed.port not in (None, 443):
        raise ValueError(f"Invalid subscribe port '{parsed.port}'; port 443 required")
    host = parsed.hostname or ""
    if not SNS_HOST_REGEX.match(host):
        raise ValueError(f"Untrusted subscribe host '{host}'; must match amazonaws.com SNS domain")


def build_sns_canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Assembles the canonical byte string for AWS SNS message signing according to AWS specification."""
    msg_type = payload.get("Type")
    if msg_type == "Notification":
        fields = ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"]
    elif msg_type in {"SubscriptionConfirmation", "UnsubscribeConfirmation"}:
        fields = ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"]
    else:
        raise ValueError(f"Unsupported SNS message type: {msg_type}")

    lines: list[str] = []
    for k in sorted(fields):
        # According to AWS SNS docs, Subject is only included if it is present and not None
        if k in payload and payload[k] is not None:
            lines.append(k)
            lines.append(str(payload[k]))

    return ("\n".join(lines) + "\n").encode("utf-8")


def _der_read_tlv(data: bytes, pos: int = 0) -> tuple[int, bytes, int]:
    """Lightweight ASN.1 DER TLV (Tag-Length-Value) reader."""
    if pos >= len(data):
        raise ValueError("Unexpected EOF in DER data")
    tag = data[pos]
    pos += 1
    if pos >= len(data):
        raise ValueError("Unexpected EOF in DER length byte")
    length_byte = data[pos]
    pos += 1
    if length_byte < 0x80:
        length = length_byte
    else:
        num_len_bytes = length_byte & 0x7F
        if num_len_bytes == 0 or pos + num_len_bytes > len(data):
            raise ValueError("Invalid DER multi-byte length")
        length = int.from_bytes(data[pos : pos + num_len_bytes], "big")
        pos += num_len_bytes
    if pos + length > len(data):
        raise ValueError("DER length exceeds available data")
    val = data[pos : pos + length]
    return tag, val, pos + length


def _extract_rsa_key_from_cert_pem(pem: str | bytes) -> tuple[int, int]:
    """Extracts RSA public key (n, e) from an X.509 PEM certificate using Python standard library."""
    if isinstance(pem, bytes):
        pem_str = pem.decode("utf-8", errors="replace")
    else:
        pem_str = pem

    b64_lines = [
        line.strip()
        for line in pem_str.strip().splitlines()
        if not line.startswith("-----")
    ]
    der_bytes = base64.b64decode("".join(b64_lines))

    # Outer Certificate SEQUENCE
    tag, cert_val, _ = _der_read_tlv(der_bytes, 0)
    if tag != 0x30:
        raise ValueError("Expected Certificate SEQUENCE")

    # tbsCertificate is the first item inside Certificate
    tag, tbs_val, _ = _der_read_tlv(cert_val, 0)
    if tag != 0x30:
        raise ValueError("Expected TBSCertificate SEQUENCE")

    # Search for SubjectPublicKeyInfo in tbsCertificate
    pos = 0
    spki_bits: bytes | None = None
    while pos < len(tbs_val):
        t, v, pos = _der_read_tlv(tbs_val, pos)
        if t == 0x30:  # SEQUENCE
            try:
                sub_pos = 0
                t_algo, v_algo, sub_pos = _der_read_tlv(v, 0)
                t_bits, v_bits, _ = _der_read_tlv(v, sub_pos)
                # RSA encryption OID is 1.2.840.113549.1.1.1 (\x2a\x86\x48\x86\xf7\x0d\x01\x01\x01)
                if t_algo == 0x30 and t_bits == 0x03 and b"\x2a\x86\x48\x86\xf7\x0d\x01\x01\x01" in v_algo:
                    spki_bits = v_bits
                    break
            except Exception:
                continue

    if spki_bits is None:
        raise ValueError("Could not find RSA SubjectPublicKeyInfo in certificate")

    # First byte of BIT STRING is unused bits (should be 0)
    rsa_pub_der = spki_bits[1:]
    _, seq_val, _ = _der_read_tlv(rsa_pub_der, 0)
    _, v_mod, sub_pos = _der_read_tlv(seq_val, 0)
    _, v_exp, _ = _der_read_tlv(seq_val, sub_pos)

    modulus = int.from_bytes(v_mod, "big")
    exponent = int.from_bytes(v_exp, "big")
    return modulus, exponent


def register_sns_certificate(url: str, cert_pem: str | bytes) -> None:
    """Registers an SNS signing certificate in the in-memory cache for deterministic testing without network calls."""
    validate_signing_cert_url(url)
    if HAS_CRYPTOGRAPHY:
        pem_bytes = cert_pem.encode("utf-8") if isinstance(cert_pem, str) else cert_pem
        cert_obj = x509.load_pem_x509_certificate(pem_bytes, default_backend())
        _CERT_CACHE[url] = cert_obj
    else:
        modulus, exponent = _extract_rsa_key_from_cert_pem(cert_pem)
        _CERT_CACHE[url] = (modulus, exponent)


def clear_sns_certificate_cache() -> None:
    """Clears the in-memory certificate cache."""
    _CERT_CACHE.clear()


def get_sns_certificate(url: str) -> Any:
    """Retrieves or fetches and caches an X.509 certificate for the given validated URL."""
    validate_signing_cert_url(url)
    if url in _CERT_CACHE:
        return _CERT_CACHE[url]

    # Bound cache size
    if len(_CERT_CACHE) >= MAX_CERT_CACHE_SIZE:
        _CERT_CACHE.clear()

    resp = httpx.get(url, timeout=5.0, follow_redirects=False)
    resp.raise_for_status()
    cert_data = resp.content

    if HAS_CRYPTOGRAPHY:
        cert_obj = x509.load_pem_x509_certificate(cert_data, default_backend())
        _CERT_CACHE[url] = cert_obj
        return cert_obj
    else:
        modulus, exponent = _extract_rsa_key_from_cert_pem(cert_data)
        _CERT_CACHE[url] = (modulus, exponent)
        return modulus, exponent


def verify_sns_signature(payload: dict[str, Any]) -> bool:
    """Cryptographically verifies an AWS SNS message signature against its SigningCertURL.

    Uses the cryptography library if installed, or falls back to standard library RSA PKCS#1 v1.5 verification.
    """
    if not settings.sns_verify_signature:
        return True

    sig_b64 = payload.get("Signature")
    cert_url = payload.get("SigningCertURL")
    sig_ver = str(payload.get("SignatureVersion", "2"))

    if not sig_b64 or not cert_url:
        return False

    try:
        sig_bytes = base64.b64decode(sig_b64)
        canonical_bytes = build_sns_canonical_bytes(payload)
        cert_or_key = get_sns_certificate(cert_url)

        if HAS_CRYPTOGRAPHY:
            public_key = cert_or_key.public_key()
            hash_algo = hashes.SHA256() if sig_ver == "2" else hashes.SHA1()
            public_key.verify(sig_bytes, canonical_bytes, padding.PKCS1v15(), hash_algo)
            return True
        else:
            modulus, exponent = cert_or_key
            k = (modulus.bit_length() + 7) // 8
            if len(sig_bytes) != k:
                return False

            sig_int = int.from_bytes(sig_bytes, "big")
            if sig_int < 0 or sig_int >= modulus:
                return False

            recovered_em = pow(sig_int, exponent, modulus).to_bytes(k, "big")

            if sig_ver == "2":
                # SHA-256 DigestInfo prefix (RFC 8017 §9.2)
                digest_info_prefix = bytes.fromhex("3031300d060960864801650304020105000420")
                h = hashlib.sha256(canonical_bytes).digest()
            else:
                # SHA-1 DigestInfo prefix
                digest_info_prefix = bytes.fromhex("3021300906052b0e03021a05000414")
                h = hashlib.sha1(canonical_bytes).digest()

            t = digest_info_prefix + h
            expected_em = b"\x00\x01" + b"\xff" * (k - 3 - len(t)) + b"\x00" + t
            return hmac.compare_digest(recovered_em, expected_em)

    except Exception:
        return False
