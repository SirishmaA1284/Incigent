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
from repo_inspection.redaction import redact_code


class NotAGitRepository(Exception):
    pass


class UnsafeGitDirectory(Exception):
    """The repository's Git metadata (gitdir/commondir) resolves outside the
    configured root, so Git inspection is refused (fail closed)."""


MAX_GIT_METADATA_ENTRIES = 200_000


def _inside(root_resolved: Path, path: Path, what: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise UnsafeGitDirectory(f"{what} could not be resolved: {str(path)!r}") from None
    if not is_within(root_resolved, resolved):
        raise UnsafeGitDirectory(f"{what} resolves outside the repository root: {str(path)!r}")
    return resolved


def _pointer(file: Path) -> str:
    """First line of a small in-repository pointer file (gitfile / commondir)."""
    if file.stat().st_size > 4096:
        raise UnsafeGitDirectory(f"{file.name} pointer file is unexpectedly large")
    lines = file.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    if not lines or not lines[0].strip() or lines[0].strip() == "gitdir:":
        raise UnsafeGitDirectory(f"{file.name} pointer file is empty")
    return lines[0].strip()


def is_bare_layout(root: Path) -> bool:
    """dulwich 0.22.8's own layout decision (Repo.__init__): unless `.git` is a
    file or `.git/objects` is a directory, a root with `objects/` and `refs/`
    is opened as a BARE repository (control dir = the root itself)."""
    dot_git = Path(root) / ".git"
    if os.path.isfile(dot_git) or os.path.isdir(dot_git / "objects"):
        return False
    return os.path.isdir(Path(root) / "objects") and os.path.isdir(Path(root) / "refs")


def _validate_layout(root: Path) -> None:
    """Validate the Git layout BEFORE dulwich opens anything.

    Verified against dulwich 0.22.8: Repo() follows a `.git` gitfile (`gitdir:`),
    a `commondir` file and `objects/info/alternates` (RECURSIVELY: every
    alternate object store reads its own info/alternates), and reads through
    any link/junction inside the metadata directories. Checking only after
    Repo() returns (as before) still let the constructor read the redirected
    config. Here every pointer is resolved by the application first, and only
    in-root pointer files are ever read.

    Refused outright: bare layouts (the metadata would be the inspected tree
    itself) and ANY object alternates, because an in-root alternate store can
    chain to, or link to, object stores outside the root."""
    root_resolved = root.resolve()
    dot_git = root / ".git"
    if is_bare_layout(root):
        raise UnsafeGitDirectory("bare repository layouts are not supported for inspection")
    if not os.path.lexists(dot_git):
        return                                   # not a Git repository: Repo() will say so
    if dot_git.is_file():
        line = _pointer(dot_git)
        if not line.startswith("gitdir:"):
            raise UnsafeGitDirectory(".git file is not a gitdir pointer")
        control = _inside(root_resolved, root / line[len("gitdir:"):].strip(), "git control directory")
    else:
        control = _inside(root_resolved, dot_git, "git control directory")
    common = control
    if (control / "commondir").is_file():
        common = _inside(root_resolved, control / _pointer(control / "commondir"), "git common directory")
    info = common / "objects" / "info"
    if os.path.lexists(info / "http-alternates"):
        raise UnsafeGitDirectory("objects/info/http-alternates is not supported")
    if os.path.lexists(info / "alternates"):
        # Not parsed at all: dulwich follows alternates recursively, so even an
        # in-root entry can chain (or link) to object stores outside the root.
        raise UnsafeGitDirectory("objects/info/alternates is not supported")
    # No link/junction inside the metadata may lead outside the root.
    seen = 0
    for base in {control, common}:
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            for name in dirnames + filenames:
                seen += 1
                if seen > MAX_GIT_METADATA_ENTRIES:
                    raise UnsafeGitDirectory("git metadata too large to validate")
                entry = Path(dirpath) / name
                try:
                    real = entry.resolve()
                except (OSError, RuntimeError):
                    raise UnsafeGitDirectory(f"git metadata entry could not be resolved: {name!r}") from None
                if not is_within(root_resolved, real):
                    raise UnsafeGitDirectory(f"git metadata entry links outside the repository root: {name!r}")


def _open(root: Path) -> Repo:
    _validate_layout(Path(root))
    try:
        repo = Repo(str(root))
    except NotGitRepository as e:
        raise NotAGitRepository(str(root)) from e
    if repo.bare:                                  # defense in depth: never inspect a bare layout
        repo.close()
        raise UnsafeGitDirectory("bare repository layouts are not supported for inspection")
    # Defense in depth: _validate_layout() already proved the layout stays in
    # the root before dulwich opened it; re-check what dulwich actually chose.
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
            summary = _s(c.message).strip().splitlines()[0][:200] if c.message and _s(c.message).strip() else ""
            out.append({
                "sha": _s(c.id),
                # Commit text is repository content: same redaction as file contents/diffs.
                "author": redact_code(_s(c.author).split(" <")[0]),   # name only; e-mail omitted
                "time": datetime.fromtimestamp(c.commit_time, timezone.utc).isoformat(),
                "summary": redact_code(summary),
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
