"""
repo_inspection/git_read.py
Read-only Git inspection using dulwich (a pure-Python Git implementation).

No `git` executable, no subprocess, and no command strings: each function is a
fixed read operation over the object store, refs and index. Nothing here writes
objects, refs, the index or the working tree. Sensitive paths are never diffed.
"""

import difflib
import os
from datetime import datetime, timezone
from pathlib import Path

from dulwich import porcelain
from dulwich.errors import NotGitRepository
from dulwich.ignore import IgnoreFilter, IgnoreFilterManager
from dulwich.repo import Repo

from repo_inspection.policy import InspectionError, InspectionLimits, is_sensitive, is_within, resolve_inside, safe_walk


class NotAGitRepository(Exception):
    pass


class UnsafeGitDirectory(Exception):
    """The repository's Git metadata (gitdir/commondir) resolves outside the
    configured root, so Git inspection is refused (fail closed)."""


def _open(root: Path) -> Repo:
    try:
        repo = Repo(str(root))
    except NotGitRepository as e:
        raise NotAGitRepository(str(root)) from e
    # A `.git` gitfile (gitdir:) or a `commondir` entry can point the control
    # and common directories anywhere (linked worktrees, submodules). Dulwich
    # honours both, so without this check a malicious repository layout could
    # redirect object/ref/index/config reads outside the configured root.
    # `core.worktree` is NOT honoured by dulwich 0.22.x (`Repo.path` is always
    # the root passed in), so only controldir/commondir need to be checked.
    root_resolved = Path(root).resolve()
    for label, path in (("control", repo.controldir()), ("common", repo.commondir())):
        try:
            resolved = Path(path).resolve(strict=True)
        except (OSError, RuntimeError):
            raise UnsafeGitDirectory(f"git {label} directory could not be resolved: {path!r}") from None
        if not is_within(root_resolved, resolved):
            raise UnsafeGitDirectory(f"git {label} directory resolves outside the repository root: {path!r}")
    return repo


def _repo_ignore_filter(root: Path, repo: Repo) -> IgnoreFilterManager:
    """Gitignore matching limited to the repository's OWN files: .git/info/exclude
    and per-directory .gitignore files under the root.

    Global ignore files and core.excludesFile are intentionally NOT honoured:
    honouring them would make inspection read arbitrary user/global paths chosen
    by the repository's own configuration, outside the configured root."""
    filters = []
    try:
        filters.append(IgnoreFilter.from_path(os.path.join(repo.controldir(), "info", "exclude")))
    except OSError:
        pass
    config = repo.get_config()
    try:
        ignorecase = config.get_boolean((b"core"), (b"ignorecase"), False)
    except KeyError:
        ignorecase = False
    return IgnoreFilterManager(str(root), filters, ignorecase)


def _s(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def head_info(root: Path) -> dict:
    with _open(root) as repo:
        try:
            chain, sha = repo.refs.follow(b"HEAD")
        except KeyError:
            chain, sha = [], None
        target = _s(chain[-1]) if chain else None
        branch = target[len("refs/heads/"):] if target and target.startswith("refs/heads/") else None
        return {"branch": branch, "head_sha": _s(sha) if sha else None, "detached": target == "HEAD"}


def status(root: Path, limits: InspectionLimits) -> dict:
    with _open(root) as repo:
        index = repo.open_index()
        tracked_count = len(index)
        if tracked_count > limits.max_tracked_files:
            # dulwich status hashes every tracked file to compare with the
            # index, so a very large repository would make inspection read and
            # hash it all. Refuse with an explicit, structured result instead
            # of silently skipping or doing unbounded work.
            return {
                "staged": {}, "unstaged": [], "untracked": [], "clean": None,
                "limited": True,
                "limit_reason": (f"{tracked_count} tracked files exceed the "
                                 f"{limits.max_tracked_files}-file status limit; "
                                 "working-tree status is unavailable"),
            }
        # dulwich's own untracked scan follows links/junctions out of the
        # repository, so untracked files come from our containment-checked walk,
        # filtered by the repository's own .gitignore rules only.
        st = porcelain.status(repo, untracked_files="no")
        tracked = {_s(p) for p in index}
        ignore = _repo_ignore_filter(root, repo)
        untracked_all = [rel for rel, _ in safe_walk(root, root)
                         if rel not in tracked and not ignore.is_ignored(rel)]
    cap = limits.max_list_entries
    staged = {k: sorted(_s(p) for p in v)[:cap] for k, v in st.staged.items()}
    unstaged = sorted(_s(p) for p in st.unstaged)[:cap]
    untracked = sorted(untracked_all)[:cap]
    clean = not (any(staged.values()) or unstaged or untracked)
    return {"staged": staged, "unstaged": unstaged, "untracked": untracked,
            "clean": clean, "limited": False}


def recent_commits(root: Path, limits: InspectionLimits, count: int = 5) -> list:
    count = max(1, min(int(count), limits.max_commits))
    out = []
    with _open(root) as repo:
        try:
            repo.head()
        except KeyError:
            return []
        for entry in repo.get_walker(max_entries=count):
            c = entry.commit
            changed = []
            for change in entry.changes():
                changes = change if isinstance(change, list) else [change]
                for ch in changes:
                    path = (ch.new.path if ch.new and ch.new.path else ch.old.path) if (ch.new or ch.old) else None
                    if path:
                        changed.append({"type": ch.type, "path": _s(path)})
            out.append({
                "sha": _s(c.id),
                "author": _s(c.author).split(" <")[0],        # name only; e-mail omitted
                "time": datetime.fromtimestamp(c.commit_time, timezone.utc).isoformat(),
                "summary": _s(c.message).strip().splitlines()[0][:200] if c.message else "",
                "changed_files": changed[:limits.max_list_entries],
            })
    return out


def working_tree_diff(root: Path, limits: InspectionLimits, only_path: str = None) -> dict:
    """Unified diff of unstaged changes to tracked files (index -> working tree).
    Read-only: blobs come from the object store; files are only read."""
    diffs, used, truncated, skipped = [], 0, False, []
    with _open(root) as repo:
        if len(repo.open_index()) > limits.max_tracked_files:
            return {"diffs": [], "truncated": True,
                    "skipped": [{"path": None, "reason": "status_limited",
                                 "detail": f"more than {limits.max_tracked_files} tracked "
                                           "files; working-tree diff unavailable"}]}
        st = porcelain.status(repo, untracked_files="no")
        index = repo.open_index()
        for raw in sorted(st.unstaged):
            rel = _s(raw)
            if only_path and rel != only_path:
                continue
            if is_sensitive(rel):
                skipped.append({"path": rel, "reason": "sensitive"})
                continue
            try:
                old = repo.object_store[index[raw].sha].data.decode("utf-8", "replace")
            except KeyError:
                old = ""
            try:
                # Same containment rule as read_file: a tracked symlink/junction
                # pointing outside the repository is never followed.
                file_path = resolve_inside(root, rel, limits)
                if is_sensitive(file_path.relative_to(root).as_posix()):
                    skipped.append({"path": rel, "reason": "sensitive"})
                    continue
                if file_path.stat().st_size > limits.max_file_bytes:
                    skipped.append({"path": rel, "reason": "too_large"})
                    continue
                new = file_path.read_text(encoding="utf-8", errors="replace")
            except InspectionError as e:
                if e.code != "not_found":
                    skipped.append({"path": rel, "reason": e.code})
                    continue
                new = ""   # deleted from the working tree
            text = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                fromfile=f"a/{rel}", tofile=f"b/{rel}"))
            if used + len(text) > limits.max_diff_bytes:
                truncated = True
                break
            used += len(text)
            diffs.append({"path": rel, "diff": text})
    return {"diffs": diffs, "skipped": skipped, "truncated": truncated}
