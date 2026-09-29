"""
repo_inspection/redaction.py
Secret redaction tuned for SOURCE CODE shown as repository evidence.

The general-purpose redactor (memory/redact.py) treats any `token:`/`password =`
as a secret assignment, which on code mangles ordinary syntax (e.g. a
`def f(token):` line) and misleads the analyst. Here, key/value redaction only
applies when a sensitive name is assigned a quoted literal on the same line;
well-known credential formats are always redacted.
"""

import re

REDACTED = "[REDACTED]"

_RULES = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), REDACTED),
    (re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]{16,}"), "Bearer " + REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(@)", re.I), r"\1" + REDACTED + r"\2"),
    # NAME = "literal"   /   "name": "literal"   (same line, literal >= 4 chars)
    (re.compile(r"(?i)(\b[\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key"
                r"|client[_-]?secret|credential)[\w.-]*[\"']?[ \t]*[:=][ \t]*)([\"'])([^\"'\n]{4,})\2"),
     r"\1\2" + REDACTED + r"\2"),
]


def redact_code(text: str) -> str:
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text
