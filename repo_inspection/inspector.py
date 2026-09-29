"""
repo_inspection/inspector.py
Read-only inspection of ONE explicitly configured target repository.

The inspector exposes narrow read operations only. It has no method that
writes, deletes, renames, moves, changes permissions, or changes Git state, and
it never runs commands. Every path is resolved and must stay inside the
configured root after following symlinks/junctions. Sensitive files are denied.
"""

import os
from pathlib import Path
from typing import Optional

from repo_inspection import git_read
from repo_inspection.git_read import UnsafeGitDirectory
from repo_inspection.redaction import redact_code as redact
from repo_inspection.evidence import FileEvidence, GitEvidence
from repo_inspection.policy import (
    InspectionError, InspectionLimits, is_sensitive, is_within, resolve_inside, safe_walk,
    validate_relative_path,
)

TEST_CONFIG_FILES = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py",
                     "package.json", "jest.config.js", "go.mod", "Cargo.toml")
TEST_DIR_NAMES = ("tests", "test", "__tests__", "spec")


class RepositoryInspector:
    def __init__(self, root, *, repository_id: str = "target", limits: Optional[InspectionLimits] = None):
        if root is None or not str(root).strip():
            raise InspectionError("root_required", "a target repository root must be configured explicitly")
        path = Path(root)
        if not path.is_absolute():
            raise InspectionError("root_not_absolute", "the repository root must be an absolute path")
        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, OSError):
            raise InspectionError("root_not_found", "the configured repository does not exist")
        if not resolved.is_dir():
            raise InspectionError("root_not_directory", "the configured repository is not a directory")
        self.root = resolved
        self.repository_id = repository_id
        self.limits = limits or InspectionLimits()

    # ── helpers ───────────────────────────────────────────────────────────────
    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."

    def _checked_file(self, rel_path: str) -> Path:
        requested = validate_relative_path(rel_path, self.limits)
        if is_sensitive(requested):
            raise InspectionError("sensitive_file", "sensitive files are denied by default")
        resolved = resolve_inside(self.root, requested, self.limits)
        if is_sensitive(self._rel(resolved)):        # e.g. harmless name linking to .env
            raise InspectionError("sensitive_file", "sensitive files are denied by default")
        return resolved

    # ── read operations ──────────────────────────────────────────────────────
    def repository_metadata(self) -> dict:
        meta = {"repository_id": self.repository_id, "repository_root": str(self.root),
                "is_git": (self.root / ".git").exists(), "limits": self.limits.to_dict()}
        if meta["is_git"]:
            try:
                meta.update(git_read.head_info(self.root))
            except git_read.NotAGitRepository:
                meta["is_git"] = False
            except UnsafeGitDirectory as e:
                # gitdir/commondir resolves outside the root: refuse, don't follow
                meta["is_git"] = False
                meta["git_unsafe"] = str(e)
        return meta

    def list_files(self, path: str = ".", max_entries: Optional[int] = None) -> dict:
        if max_entries is not None and (isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1):
            raise InspectionError("invalid_limit", "max_entries must be a positive integer")
        cap = min(max_entries or self.limits.max_list_entries, self.limits.max_list_entries)
        start = resolve_inside(self.root, path, self.limits)
        if not start.is_dir():
            raise InspectionError("not_a_directory", "path is not a directory")
        entries, truncated = [], False
        walker = safe_walk(self.root, start)
        skipped_links = 0
        while True:
            try:
                rel, real = next(walker)
            except StopIteration as done:
                skipped_links = done.value or 0
                break
            if len(entries) >= cap:
                truncated = True
                break
            entries.append({"path": rel, "size": real.stat().st_size, "sensitive": is_sensitive(rel)})
        return {"path": self._rel(start), "entries": entries, "truncated": truncated,
                "skipped_outside_root": skipped_links}

    def read_file(self, path: str, start_line: int = 1, end_line: Optional[int] = None) -> FileEvidence:
        if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
            raise InspectionError("invalid_range", "start_line must be a positive integer")
        if end_line is not None and (isinstance(end_line, bool) or not isinstance(end_line, int) or end_line < start_line):
            raise InspectionError("invalid_range", "end_line must be an integer >= start_line")
        file_path = self._checked_file(path)
        if not file_path.is_file():
            raise InspectionError("not_a_file", "path is not a regular file")
        rel, size = self._rel(file_path), file_path.stat().st_size
        if size > self.limits.max_file_bytes:
            return FileEvidence(rel, start_line, start_line - 1, "", size, 0, truncated=True,
                                truncation_reason=f"file exceeds {self.limits.max_file_bytes} bytes; not read")
        raw = file_path.read_bytes()
        if b"\x00" in raw[:8192]:
            return FileEvidence(rel, start_line, start_line - 1, "", size, 0, truncated=True,
                                truncation_reason="binary file; not read")
        lines = raw.decode("utf-8", "replace").splitlines()
        wanted_end = len(lines) if end_line is None else min(end_line, len(lines))
        last = min(wanted_end, start_line + self.limits.max_lines - 1)
        truncated = last < wanted_end
        content = "\n".join(lines[start_line - 1:last])
        safe = redact(content)
        return FileEvidence(rel, start_line, last, safe, size, len(lines), redacted=safe != content,
                            truncated=truncated,
                            truncation_reason=f"limited to {self.limits.max_lines} lines" if truncated else None)

    def git_evidence(self, commit_count: int = 5) -> GitEvidence:
        if isinstance(commit_count, bool) or not isinstance(commit_count, int) or commit_count < 1:
            raise InspectionError("invalid_limit", "count must be a positive integer")
        try:
            head = git_read.head_info(self.root)
            st = git_read.status(self.root, self.limits)
            commits = git_read.recent_commits(self.root, self.limits, commit_count)
        except git_read.NotAGitRepository:
            raise InspectionError("not_git", "the target repository is not a Git repository")
        except UnsafeGitDirectory as e:
            raise InspectionError("unsafe_gitdir", str(e))
        if st.get("limited"):
            return GitEvidence(branch=head["branch"], head_sha=head["head_sha"], clean=None,
                               status=st, recent_commits=commits, changed_files=[], truncated=True,
                               limited=True, limit_reason=st.get("limit_reason"))
        changed = sorted({*st["unstaged"], *[p for v in st["staged"].values() for p in v],
                          *[f["path"] for c in commits for f in c["changed_files"]]})
        return GitEvidence(branch=head["branch"], head_sha=head["head_sha"], clean=st["clean"],
                           status={k: st[k] for k in ("staged", "unstaged", "untracked")},
                           recent_commits=commits, changed_files=changed[:self.limits.max_list_entries],
                           truncated=len(changed) > self.limits.max_list_entries)

    def diff(self, path: Optional[str] = None) -> dict:
        if path is not None:
            path = validate_relative_path(path, self.limits)
        try:
            result = git_read.working_tree_diff(self.root, self.limits, only_path=path)
        except git_read.NotAGitRepository:
            raise InspectionError("not_git", "the target repository is not a Git repository")
        except UnsafeGitDirectory as e:
            raise InspectionError("unsafe_gitdir", str(e))
        for d in result["diffs"]:
            safe = redact(d["diff"])
            d["redacted"], d["diff"] = safe != d["diff"], safe
        return result

    def test_configuration(self) -> dict:
        configs = [n for n in TEST_CONFIG_FILES if (self.root / n).is_file() and is_within(self.root, (self.root / n).resolve())]
        test_files = []
        for d in TEST_DIR_NAMES:
            if (self.root / d).is_dir():
                listing = self.list_files(d)
                test_files += [e["path"] for e in listing["entries"] if not e["sensitive"]]
        return {"config_files": configs, "test_directories": [d for d in TEST_DIR_NAMES if (self.root / d).is_dir()],
                "test_files": test_files[:self.limits.max_list_entries]}


# ── Allowlisted structured operations (the only request interface) ────────────
OPERATIONS = {
    "inspect_repository": set(),
    "list_files": {"path", "max_entries"},
    "read_file": {"path", "start_line", "end_line"},
    "inspect_git_status": set(),
    "inspect_recent_commits": {"count"},
    "inspect_diff": {"path"},
    "inspect_test_configuration": set(),
}


def run_operation(inspector: RepositoryInspector, request) -> dict:
    """Execute ONE allowlisted read operation described by a structured request.
    Unknown operations and unexpected fields (e.g. "command") are rejected."""
    if not isinstance(request, dict):
        raise InspectionError("invalid_request", "request must be an object")
    op = request.get("operation")
    if op not in OPERATIONS:
        raise InspectionError("unknown_operation", f"unsupported operation {op!r}")
    extra = set(request) - {"operation"} - OPERATIONS[op]
    if extra:
        raise InspectionError("unexpected_fields", f"unexpected fields: {sorted(extra)}")
    args = {k: v for k, v in request.items() if k != "operation"}
    if op == "inspect_repository":
        return inspector.repository_metadata()
    if op == "list_files":
        return inspector.list_files(**args)
    if op == "read_file":
        return inspector.read_file(**args).__dict__
    if op == "inspect_git_status":
        ev = inspector.git_evidence(commit_count=1)
        return {"branch": ev.branch, "head_sha": ev.head_sha, "clean": ev.clean, "status": ev.status,
                "limited": ev.limited, "limit_reason": ev.limit_reason}
    if op == "inspect_recent_commits":
        return {"recent_commits": inspector.git_evidence(commit_count=args.get("count", 5)).recent_commits}
    if op == "inspect_diff":
        return inspector.diff(**args)
    return inspector.test_configuration()
