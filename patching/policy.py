"""
patching/policy.py
Limits and path validation for Stage 6 patch preparation.

Everything here is pure: no filesystem writes, no commands. Path validation is
NOT duplicated: the Stage 5 repository policy (repo_inspection.policy) is the
single source of truth for lexical validation, containment and sensitive-file
denial; this module only adds patch-specific limits, the worktree-local
re-check (the same rules applied against the isolated worktree root), the
hostile-tree-entry check used during materialization, and the workspace
placement check.
"""

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from repo_inspection.policy import (
    InspectionError, InspectionLimits, is_sensitive, is_within, resolve_inside,
    validate_relative_path,
)


class PatchError(Exception):
    """A patch request was refused. `code` is stable and machine-readable."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class PatchLimits:
    # Stage 6 patches are single-file by design (one "modify" operation on one
    # file); that is enforced structurally by the intent schema, not a setting.
    max_lines_added: int = 100
    max_lines_removed: int = 100
    max_diff_bytes: int = 32 * 1024
    max_old_content_chars: int = 8_000   # expected_old_content size cap
    max_replacement_chars: int = 8_000   # replacement_content size cap
    max_worktree_files: int = 5_000      # materialization guards (pinned tree)
    max_worktree_bytes: int = 50 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "PatchLimits":
        def env_int(name, default):
            try:
                value = int(os.getenv(name, ""))
                return value if value > 0 else default
            except ValueError:
                return default
        d = cls()
        return cls(
            max_lines_added=env_int("PATCH_MAX_LINES_ADDED", d.max_lines_added),
            max_lines_removed=env_int("PATCH_MAX_LINES_REMOVED", d.max_lines_removed),
            max_diff_bytes=env_int("PATCH_MAX_DIFF_BYTES", d.max_diff_bytes),
            max_old_content_chars=env_int("PATCH_MAX_OLD_CHARS", d.max_old_content_chars),
            max_replacement_chars=env_int("PATCH_MAX_REPLACEMENT_CHARS", d.max_replacement_chars),
        )

    def to_dict(self) -> dict:
        return asdict(self)


def validate_patch_path(rel_path, root, limits: InspectionLimits):
    """Validate one patch target path against a root (the ORIGINAL repository
    or the isolated worktree): lexical checks, containment after resolution,
    sensitive-file denial. Returns the resolved path inside root."""
    clean = validate_relative_path(rel_path, limits)
    if is_sensitive(clean):
        raise InspectionError("sensitive_file", "sensitive files cannot be patched")
    resolved = resolve_inside(root, clean, limits)
    if not resolved.is_file():
        raise InspectionError("not_a_file", "patch target is not a regular file")
    return resolved


def validate_patch_target_against_worktree(rel_path, worktree, limits: InspectionLimits):
    """Map a validated repository-relative path into the isolated worktree and
    re-apply containment there. The final write path is ALWAYS derived here
    (worktree root + validated relative path); the model never supplies one."""
    clean = validate_relative_path(rel_path, limits)
    if is_sensitive(clean):
        raise InspectionError("sensitive_file", "sensitive files cannot be patched")
    resolved = resolve_inside(worktree.root, clean, limits)
    if not is_within(worktree.root, resolved):
        raise InspectionError("outside_root", "patch target resolves outside the worktree")
    return resolved


# ── Path relationships (case-insensitive where the OS is) ────────────────────

def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def path_contains(outer, inner) -> bool:
    """True if `inner` is `outer` or below it. Compares normalized absolute
    paths component-wise (never string prefixes); different drives -> False."""
    o, i = _norm(outer), _norm(inner)
    try:
        return os.path.commonpath([o, i]) == o
    except ValueError:
        return False


def path_strictly_contains(outer, inner) -> bool:
    """True only if `inner` is strictly below `outer` (never equal)."""
    return path_contains(outer, inner) and _norm(outer) != _norm(inner)


def paths_overlap(a, b) -> bool:
    """a == b, a below b, or b below a."""
    return path_contains(a, b) or path_contains(b, a)


def _forms(path) -> list:
    """The absolute form AND the link-resolved form of a path (it may not exist
    yet; resolve() then resolves the existing prefix, e.g. through a junction)."""
    p = Path(path)
    forms = [Path(os.path.abspath(str(p)))]
    try:
        forms.append(p.resolve())
    except (OSError, RuntimeError):
        pass
    return forms


def application_root() -> Path:
    """The application's own repository root, or — when the application is not
    a Git checkout — its source directory. Never a valid patch workspace or
    (without an explicit override) patch target."""
    source = Path(__file__).resolve().parent.parent
    try:
        from dulwich.repo import Repo
        repo = Repo.discover(str(source))
    except Exception:
        return source
    try:
        return Path(repo.path).resolve()
    finally:
        try:
            repo.close()
        except Exception:
            pass


def validate_workspace(workspace, *, target_root, app_root=None) -> Path:
    """Refuse a patch workspace that is, contains, or is inside the target
    repository or the application's own repository. Pure: checks run on
    resolved AND absolute forms before anything is created. Returns the
    resolved workspace path."""
    if workspace is None or not str(workspace).strip():
        raise PatchError("workspace_unsafe", "no patch workspace configured")
    app_root = app_root if app_root is not None else application_root()
    protected = [("target repository", target_root), ("application repository", app_root)]
    for w in _forms(workspace):
        for label, root in protected:
            if root is None:
                continue
            for r in _forms(root):
                if paths_overlap(w, r):
                    raise PatchError("workspace_unsafe",
                                     f"PATCH_WORKSPACE must not be, contain or be inside the {label}")
    return Path(workspace).resolve()


# ── Git tree entry names (attacker-controlled) ───────────────────────────────
# A tree entry name is ONE path component chosen by whoever wrote the
# repository. Git allows almost any byte except "/" and NUL, but on Windows
# "\" and ":" are path syntax (separators, drive letters, NTFS streams), so a
# name like "..\x" or "C:\x" would escape the worktree if joined naively.
_FORBIDDEN_CHARS = set('\\/:<>"|?*')
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_RESERVED_WIN = re.compile(
    r"^(con|prn|aux|nul|com[0-9\u00b9\u00b2\u00b3]|lpt[0-9\u00b9\u00b2\u00b3]|conin\$|conout\$)\s*(\..*)?$",
    re.I)
_GIT_SHORTNAME = re.compile(r"^git~\d+$", re.I)
MAX_COMPONENT_BYTES = 255


def check_tree_component(raw: bytes) -> str:
    """Validate one Git tree entry name. Returns the decoded name, or raises
    PatchError("unsafe_tree_entry"). Never silently skips: an unsafe name makes
    the whole materialization fail closed."""
    shown = repr(raw[:80])
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PatchError("unsafe_tree_entry", f"tree entry {shown} is not valid UTF-8")
    if not name or len(raw) > MAX_COMPONENT_BYTES:
        raise PatchError("unsafe_tree_entry", f"tree entry {shown} is empty or too long")
    if name in (".", ".."):
        raise PatchError("unsafe_tree_entry", f"tree entry {shown} is a relative path component")
    if any(c in _FORBIDDEN_CHARS for c in name) or _CONTROL.search(name):
        raise PatchError("unsafe_tree_entry",
                         f"tree entry {shown} contains a path separator, drive/stream "
                         f"separator, reserved or control character")
    if name.endswith((" ", ".")):
        raise PatchError("unsafe_tree_entry", f"tree entry {shown} ends with a space or dot")
    if name.lower() == ".git" or _GIT_SHORTNAME.match(name):
        raise PatchError("unsafe_tree_entry", f"tree entry {shown} names Git metadata")
    if _RESERVED_WIN.match(name):
        raise PatchError("unsafe_tree_entry", f"tree entry {shown} is a Windows device name")
    return name
