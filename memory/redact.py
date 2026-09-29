"""
memory/redact.py
Small, conservative secret redaction applied before anything is sent to
long-term memory. It targets obvious credentials; it is not a DLP system.
"""

import re

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = r"(?:password|passwd|pwd|secret|client[_-]?secret|token|access[_-]?token|refresh[_-]?token|api[_-]?key|access[_-]?key|private[_-]?key|credentials?)"

# Order matters: specific token shapes first, then generic key=value forms.
_RULES = [
    # PEM private key blocks
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), REDACTED),
    # Authorization headers and bearer tokens
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:bearer|basic|token)?\s*[A-Za-z0-9._~+/=-]+"), r"\1" + REDACTED),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer " + REDACTED),
    # Well-known token formats
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),                       # OpenAI-style keys
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),                            # AWS access key IDs
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),                    # GitHub tokens
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), REDACTED),                 # Slack tokens
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),  # JWTs
    # Credentials embedded in URLs: scheme://user:password@host
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(@)", re.I), r"\1" + REDACTED + r"\2"),
    # key=value / key: value / "key": "value" with a sensitive key name
    (re.compile(r"(?i)([\"']?\b[\w.-]*" + _SENSITIVE_KEY + r"[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&}]+)"),
     r"\1" + REDACTED),
]

_SENSITIVE_KEY_NAME = re.compile(r"(?i)^[\w.-]*" + _SENSITIVE_KEY + r"$")


def redact(text: str) -> str:
    """Return text with obvious secrets replaced by [REDACTED]."""
    if not text:
        return text
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def redact_mapping(values: dict) -> dict:
    """Redact every value; values under a sensitive key name are dropped entirely."""
    out = {}
    for key, value in values.items():
        if _SENSITIVE_KEY_NAME.match(str(key)):
            out[key] = REDACTED
        else:
            out[key] = redact(str(value))
    return out
