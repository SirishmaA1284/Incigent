"""
patching/apply.py
Narrow, application-controlled patch application inside an isolated worktree.

The model never supplies a filesystem path or a command. The application
derives every write path from (validated repository-relative path + worktree
root). The complete resulting content and its diff are computed IN MEMORY and
checked against every limit BEFORE anything is written; if anything is
ambiguous or out of scope, nothing is modified. The original repository is
never touched.

Encoding / line endings: only files that are valid UTF-8 can be patched, and
the substitution is done on the exact bytes (the single occurrence of
expected_old_content's UTF-8 bytes is replaced by replacement_content's bytes),
so no other byte of the file can change. Line endings are NOT normalized: a
file committed with CRLF matches only an expected_old_content that contains
the same CRLF sequences, otherwise the patch is rejected as "not found".
"""

import difflib
import hashlib

from repo_inspection.policy import InspectionError, InspectionLimits

from patching.policy import PatchError, validate_patch_target_against_worktree
from patching.worktree import Worktree


def _base_blob(repo, base_sha: str, rel: str) -> bytes:
    """The exact bytes of `rel` at the pinned base commit (read-only)."""
    commit = repo.object_store[base_sha.encode("ascii")]
    parts = rel.split("/")
    tree_id = commit.tree
    for i, part in enumerate(parts):
        tree = repo.object_store[tree_id]
        key = part.encode("utf-8")
        if key not in tree:
            raise PatchError("patch_rejected", f"{rel} does not exist at the pinned base")
        mode, sha = tree[key]
        if mode == 0o40000 and i == len(parts) - 1:
            raise PatchError("patch_rejected", f"{rel} does not exist at the pinned base")
        if i == len(parts) - 1:
            if mode == 0o160000 or mode == 0o120000:
                raise PatchError("patch_rejected", f"{rel} is not a regular text file")
            return bytes(repo.object_store[sha].data)
        tree_id = sha
    raise PatchError("patch_rejected", f"{rel} does not exist at the pinned base")


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _utf8(data: bytes, rel: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise PatchError("patch_rejected",
                         f"{rel}: not valid UTF-8; only UTF-8 text files can be patched")


def diff_stats(rel: str, old: bytes, new: bytes) -> dict:
    """Unified diff of two UTF-8 byte strings with its true added/removed line
    counts and size. Pure (no I/O)."""
    text_diff = "".join(difflib.unified_diff(
        _utf8(old, rel).splitlines(True), _utf8(new, rel).splitlines(True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}"))
    lines = text_diff.splitlines()
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return {"added": added, "removed": removed, "diff_bytes": len(text_diff.encode("utf-8"))}


def _check_limits(stats: dict, limits) -> None:
    if stats["added"] > limits.max_lines_added:
        raise PatchError("patch_rejected", f"patch adds {stats['added']} lines (limit {limits.max_lines_added})")
    if stats["removed"] > limits.max_lines_removed:
        raise PatchError("patch_rejected",
                         f"patch removes {stats['removed']} lines (limit {limits.max_lines_removed})")
    if stats["diff_bytes"] > limits.max_diff_bytes:
        raise PatchError("patch_rejected",
                         f"patch diff is {stats['diff_bytes']} bytes (limit {limits.max_diff_bytes})")


def validate_operations(worktree: Worktree, intent, limits, path_limits: InspectionLimits,
                        repo=None, base_sha=None) -> dict:
    """Validate the whole patch against the worktree BEFORE writing anything:
    path policy re-applied against the worktree root, the file is UTF-8 text
    identical to the pinned base, expected old content occurs exactly once,
    and the RESULTING content's true diff is within every limit. Returns the
    resolved write path, the exact new bytes and the diff stats."""
    try:
        resolved = validate_patch_target_against_worktree(intent.file, worktree, path_limits)
    except InspectionError as e:
        raise PatchError("patch_rejected", f"{intent.file}: {e.code}")
    raw = resolved.read_bytes()
    if _is_binary(raw):
        raise PatchError("patch_rejected", f"{intent.file}: binary files cannot be patched")
    if repo is not None and base_sha is not None:
        base = _base_blob(repo, base_sha, intent.file)
        if _is_binary(base):
            raise PatchError("patch_rejected", f"{intent.file}: binary files cannot be patched")
        if base != raw:
            raise PatchError("patch_rejected", f"{intent.file}: candidate differs from the pinned base")
    _utf8(raw, intent.file)
    if "\x00" in intent.replacement_content:
        raise PatchError("patch_rejected", "replacement_content must be text")

    old = intent.expected_old_content.encode("utf-8")
    new = intent.replacement_content.encode("utf-8")
    occurrences = raw.count(old)
    if occurrences == 0:
        hint = ""
        if b"\r\n" in raw and b"\r" not in old:
            hint = (" (the file uses CRLF line endings; expected_old_content must match "
                    "byte-for-byte and line endings are not normalized)")
        raise PatchError("patch_rejected", f"expected_old_content not found in {intent.file}{hint}")
    if occurrences > 1:
        raise PatchError("patch_rejected", f"expected_old_content is ambiguous ({occurrences} matches)")

    new_bytes = raw.replace(old, new, 1)
    stats = diff_stats(intent.file, raw, new_bytes)
    _check_limits(stats, limits)
    return {"path": resolved, "new_bytes": new_bytes, "original_bytes": raw, **stats}


def apply_patch(worktree: Worktree, intent, limits, *, path_limits: InspectionLimits,
                repo=None, base_sha=None) -> dict:
    """Validate EVERYTHING first (including the resulting diff), then write
    exactly the precomputed bytes. On any validation failure nothing is
    written. Writes go only to paths the application derived from the
    validated relative path + worktree root."""
    check = validate_operations(worktree, intent, limits, path_limits, repo=repo, base_sha=base_sha)
    path = check["path"]
    try:
        path.write_bytes(check["new_bytes"])
    except OSError as e:
        raise PatchError("patch_application_failed", f"write failed: {e}")
    if path.read_bytes() != check["new_bytes"]:
        raise PatchError("patch_application_failed", "written content verification failed")
    return {"path": str(path), "added": check["added"], "removed": check["removed"],
            "diff_bytes": check["diff_bytes"]}


def validate_applied_diff(worktree: Worktree, intent, limits, *, repo, base_sha) -> dict:
    """After application (defence in depth): the ONLY allowed change is the
    intended file; every other worktree file must still be byte-identical to
    the materialised base, and the diff must still be within the limits."""
    base = _base_blob(repo, base_sha, intent.file)
    current = (worktree.root / intent.file).read_bytes()
    if _is_binary(base) or _is_binary(current):
        raise PatchError("patch_rejected", f"{intent.file}: binary files cannot be patched")
    stats = diff_stats(intent.file, base, current)
    _check_limits(stats, limits)
    expected = base.replace(intent.expected_old_content.encode("utf-8"),
                            intent.replacement_content.encode("utf-8"), 1)
    if current != expected:
        raise PatchError("patch_rejected", f"unexpected changes: [{intent.file!r}]")

    unexpected = []
    for rel, sha in worktree.created_hashes.items():
        if rel == intent.file:
            continue
        p = worktree.root / rel
        try:
            content = p.read_bytes()
        except OSError:
            unexpected.append(rel)
            continue
        if hashlib.sha256(content).hexdigest() != sha:
            unexpected.append(rel)
    new_files = [str(p.relative_to(worktree.root).as_posix())
                 for p in worktree.root.rglob("*")
                 if p.is_file() and p.relative_to(worktree.root).as_posix() not in worktree.created_hashes]
    if unexpected or new_files:
        raise PatchError("patch_rejected",
                         f"unexpected changes: {sorted(set(unexpected + new_files))[:10]}")
    return {"file": intent.file, "added": stats["added"], "removed": stats["removed"],
            "binary": False, "diff_bytes": stats["diff_bytes"]}


def patch_fingerprint(intent, *, patch_id: str, base_sha: str) -> str:
    """Deterministic fingerprint of the intended change, for traceability."""
    import json as _json
    blob = _json.dumps({"patch_id": patch_id, "base_sha": base_sha, **intent.to_dict()},
                       sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()
