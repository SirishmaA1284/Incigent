"""
repo_inspection/evidence.py
Structured, traceable repository evidence.

Every collected item gets a deterministic ID in collection order: R001, R002, ...
("R" for repository, so it can never be confused with timeline event IDs like
[E12]). Each item also records a SHA-256 fingerprint of what was observed, so an
analyst statement citing [R003] can be traced to exact path, lines and content.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class FileEvidence:
    path: str
    line_start: int
    line_end: int
    content: str
    size: int
    total_lines: int
    redacted: bool = False
    truncated: bool = False
    truncation_reason: Optional[str] = None


@dataclass
class GitEvidence:
    branch: Optional[str]
    head_sha: Optional[str]
    clean: Optional[bool]
    status: dict = field(default_factory=dict)          # staged/unstaged/untracked (paths)
    recent_commits: list = field(default_factory=list)  # sha, author, time, summary, changed_files
    changed_files: list = field(default_factory=list)   # union of working-tree + recent-commit changes
    truncated: bool = False
    limited: bool = False              # working-tree status/diff refused (tracked-file limit)
    limit_reason: Optional[str] = None


@dataclass
class EvidenceItem:
    evidence_id: str        # "R001"
    kind: str               # repository | git_status | commits | diff | file | test_config
    summary: str
    data: dict
    fingerprint: str        # sha256 over canonical JSON of data

    def to_dict(self) -> dict:
        return asdict(self)


def _fingerprint(data: dict) -> str:
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


class EvidenceLog:
    """Assigns stable IDs in the order evidence is added."""

    def __init__(self, prefix: str = "R"):
        self.prefix = prefix
        self.items: list = []

    def add(self, kind: str, summary: str, data: dict) -> EvidenceItem:
        item = EvidenceItem(f"{self.prefix}{len(self.items) + 1:03d}", kind, summary, data, _fingerprint(data))
        self.items.append(item)
        return item

    def ids(self) -> list:
        return [i.evidence_id for i in self.items]

    def get(self, evidence_id: str) -> Optional[EvidenceItem]:
        return next((i for i in self.items if i.evidence_id == evidence_id), None)


@dataclass
class RepositoryInspection:
    repository_id: str
    repository_root: str
    status: str                         # "inspected" | "not_configured" | "unavailable"
    branch: Optional[str] = None
    head_sha: Optional[str] = None
    clean: Optional[bool] = None
    files_inspected: list = field(default_factory=list)
    relevant_files: list = field(default_factory=list)
    findings: list = field(default_factory=list)       # EvidenceItem dicts
    warnings: list = field(default_factory=list)
    truncated: bool = False
    inspection_limits: dict = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    def evidence_ids(self) -> list:
        return [f["evidence_id"] for f in self.findings]
