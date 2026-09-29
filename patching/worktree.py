"""
patching/worktree.py
Application-owned isolated working trees (Stage 6).

Dulwich 0.22.8 has no public worktree-creation API: its private
`_init_new_working_directory` produces a linked worktree whose `.git` gitfile
points OUTSIDE the worktree root — exactly the layout Stage 5 deliberately
refuses (`unsafe_gitdir`). Rather than introducing a `git` subprocess, Stage 6
creates an isolated working tree by materialising the pinned commit's tree
in-process from the object store into a unique application-owned directory
under a configured workspace. The directory contains NO `.git` plumbing, so it
cannot be mistaken for the target repository and no Git state can leak or be
mutated. All Git reads stay on the read-only Stage 5 interface.

Tree entry names are attacker-controlled (whoever wrote the repository chose
them). Every name is validated as a single safe component, case-insensitive
collisions are refused, every directory and file is created exclusively (never
through something that already exists), and every created path is re-resolved
and required to be strictly inside the candidate before its content is
written. Any violation fails the whole materialization closed.
"""

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dulwich.objects import Commit

from repo_inspection.git_read import NotAGitRepository, UnsafeGitDirectory, _open

from patching.policy import (
    PatchError, check_tree_component, path_strictly_contains, paths_overlap, validate_workspace,
)

GIT_MODE_DIR = 0o40000
GIT_MODE_SYMLINK = 0o120000
GIT_MODE_GITLINK = 0o160000

_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")
_WORKTREE_ID = re.compile(r"^wt-([0-9a-f]{12})$")
METADATA_NAME = "patch-worktrees.json"


def sanitize_incident_id(incident_id: str) -> str:
    """A safe directory name for an incident id (never a model-chosen path)."""
    safe = _SAFE_ID.sub("_", str(incident_id or ""))[:40].strip("_")
    return safe or "incident"


@dataclass
class Worktree:
    worktree_id: str
    root: Path
    base_sha: str
    incident_id: str
    patch_id: str
    created_hashes: dict = field(default_factory=dict)   # rel path -> sha256 at creation

    def to_dict(self) -> dict:
        return {"worktree_id": self.worktree_id, "path": str(self.root),
                "base_sha": self.base_sha, "incident_id": self.incident_id,
                "patch_id": self.patch_id}


class WorktreeManager:
    """Creates and cleans up ONLY worktrees this application owns.

    Ownership is recorded in a metadata file under the workspace root. Cleanup
    never accepts a path from a caller: it looks up an application-issued
    worktree id and treats the recorded entry as hostile — the path must be
    exactly <workspace>/<incident>/candidate-<id>, strictly below the
    workspace, not a link, and unrelated to the target repository — before
    anything is removed.

    Nothing is created on disk until create() has checked that the workspace
    does not overlap the target repository or the application repository."""

    def __init__(self, workspace: Path, *, app_root: Optional[Path] = None):
        self.workspace = Path(workspace).resolve()
        self._app_root = app_root
        self._metadata_path = self.workspace / METADATA_NAME

    # ── metadata ────────────────────────────────────────────────────────────
    def _load_metadata(self) -> dict:
        try:
            data = json.loads(self._metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_metadata(self, metadata: dict) -> None:
        tmp = self._metadata_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        tmp.replace(self._metadata_path)

    def _register(self, worktree_id: str, entry: dict) -> None:
        meta = self._load_metadata()
        meta[worktree_id] = entry
        self._save_metadata(meta)

    def _unregister(self, worktree_id: str) -> None:
        meta = self._load_metadata()
        meta.pop(worktree_id, None)
        self._save_metadata(meta)

    # ── creation ────────────────────────────────────────────────────────────
    def create(self, target_root: Path, base_sha: str, incident_id: str,
               limits, *, patch_id: Optional[str] = None) -> Worktree:
        """Materialise the pinned commit's tree into a new isolated worktree.

        target_root: the ORIGINAL repository (read-only; only the object store
        is read). base_sha: the pinned commit the candidate must be based on.
        Everything written goes into self.workspace only, and only after the
        workspace placement check has passed."""
        validate_workspace(self.workspace, target_root=target_root, app_root=self._app_root)
        patch_id = patch_id or uuid.uuid4().hex
        worktree_id = f"wt-{patch_id[:12]}"
        incident_dir = self.workspace / sanitize_incident_id(incident_id)
        worktree_dir = incident_dir / f"candidate-{patch_id[:12]}"
        if worktree_dir.exists():
            raise PatchError("worktree_exists", f"worktree path already exists: {worktree_dir}")
        try:
            incident_dir.mkdir(parents=True, exist_ok=True)
            worktree_dir.mkdir(parents=False)
            root = worktree_dir.resolve(strict=True)
        except OSError as e:
            raise PatchError("worktree_creation_failed", f"could not create worktree dir: {e}")
        if not path_strictly_contains(self.workspace, root):
            raise PatchError("worktree_creation_failed", "worktree directory resolves outside the workspace")

        try:
            repo = _open(target_root)
        except (NotAGitRepository, UnsafeGitDirectory) as e:
            shutil.rmtree(root, ignore_errors=True)
            raise PatchError("base_unavailable", f"target repository unavailable: {e}")

        try:
            commit = repo.object_store[base_sha.encode("ascii")]
        except KeyError:
            shutil.rmtree(root, ignore_errors=True)
            raise PatchError("base_unavailable", f"base commit {base_sha[:12]} not found")
        if not isinstance(commit, Commit):
            shutil.rmtree(root, ignore_errors=True)
            raise PatchError("base_unavailable", "pinned base is not a commit")

        try:
            hashes = self._materialize_tree(repo, commit.tree, root, limits)
        except PatchError:
            shutil.rmtree(root, ignore_errors=True)
            raise
        except OSError as e:
            shutil.rmtree(root, ignore_errors=True)
            raise PatchError("worktree_creation_failed", f"materialisation failed: {e}")

        self._register(worktree_id, {
            "worktree_id": worktree_id, "path": str(root),
            "base_sha": base_sha, "incident_id": str(incident_id), "patch_id": patch_id,
            "target_root": str(Path(target_root).resolve()),
            "applied": False, "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return Worktree(worktree_id=worktree_id, root=root, base_sha=base_sha,
                        incident_id=str(incident_id), patch_id=patch_id, created_hashes=hashes)

    @staticmethod
    def _target(dest: Path, here: Path, name: str) -> Path:
        """BEFORE anything is created: the destination must be exactly one
        component below the (re-resolved) current directory, and strictly
        inside the candidate. Defence in depth behind the name checks: a
        drive-, UNC- or '..'-bearing name fails here even if it got through."""
        try:
            here_real = here.resolve(strict=True)
        except (OSError, RuntimeError):
            raise PatchError("unsafe_tree_entry", "materialisation directory cannot be resolved")
        if not (os.path.normcase(str(here_real)) == os.path.normcase(str(dest))
                or path_strictly_contains(dest, here_real)):
            raise PatchError("unsafe_tree_entry", "tree path escapes the worktree")
        full = here_real / name
        lexical = Path(os.path.abspath(str(full)))
        if (os.path.normcase(str(lexical.parent)) != os.path.normcase(str(here_real))
                or not path_strictly_contains(dest, lexical)):
            raise PatchError("unsafe_tree_entry", "tree path escapes the worktree")
        return full

    @staticmethod
    def _inside(dest: Path, created: Path) -> None:
        """AFTER exclusive creation, before any content is written: re-resolve
        the new path and require it to be strictly inside the candidate."""
        try:
            real = created.resolve(strict=True)
        except (OSError, RuntimeError):
            raise PatchError("unsafe_tree_entry", "materialised path cannot be resolved")
        if not path_strictly_contains(dest, real):
            raise PatchError("unsafe_tree_entry", "tree path escapes the worktree")

    def _materialize_tree(self, repo, tree_id, dest: Path, limits) -> dict:
        """Iterative DFS over the pinned tree. Only regular files are written;
        symlink and submodule entries are NOT materialised (no links, so nothing
        in the worktree can point outside it). Every entry name is validated
        (fail closed, never skipped); file count and total bytes are bounded."""
        hashes, files, bytes_written = {}, 0, 0
        stack = [(tree_id, dest)]
        while stack:
            tid, here = stack.pop()
            tree = repo.object_store[tid]
            seen = set()
            for item in tree.items():
                name = check_tree_component(item.path)
                folded = name.casefold()
                if folded in seen:
                    # Windows (and default macOS) filesystems are case-insensitive:
                    # "A.py" and "a.py" would silently overwrite each other.
                    raise PatchError("unsafe_tree_entry",
                                     f"tree entries collide case-insensitively: {name!r}")
                seen.add(folded)
                if item.mode in (GIT_MODE_SYMLINK, GIT_MODE_GITLINK):
                    continue                      # links/submodules are never materialised
                full = self._target(dest, here, name)
                if item.mode == GIT_MODE_DIR:
                    full.mkdir(exist_ok=False)    # exclusive: never reuse an existing path
                    self._inside(dest, full)
                    stack.append((item.sha, full))
                    continue
                if files >= limits.max_worktree_files:
                    raise PatchError("worktree_creation_failed",
                                     f"pinned tree exceeds {limits.max_worktree_files} files")
                blob = repo.object_store[item.sha]
                data = bytes(blob.data)
                if bytes_written + len(data) > limits.max_worktree_bytes:
                    raise PatchError("worktree_creation_failed",
                                     f"pinned tree exceeds {limits.max_worktree_bytes} bytes")
                # Exclusive create ("x"): fails if anything (file, link) already
                # exists at the path; containment is verified before any byte
                # of content is written.
                with open(full, "xb") as fh:
                    self._inside(dest, full)
                    fh.write(data)
                rel = full.relative_to(dest).as_posix()
                hashes[rel] = hashlib.sha256(data).hexdigest()
                files += 1
                bytes_written += len(data)
        return hashes

    # ── inspection / cleanup ────────────────────────────────────────────────
    def inspect(self, worktree_id: str) -> Optional[dict]:
        entry = self._load_metadata().get(worktree_id)
        if not isinstance(entry, dict):
            return None
        path = Path(str(entry.get("path", "")))
        return {**entry, "exists": path.is_dir()}

    def list_worktrees(self) -> list:
        return sorted(self._load_metadata().keys())

    def mark_applied(self, worktree_id: str) -> None:
        meta = self._load_metadata()
        if worktree_id in meta:
            meta[worktree_id]["applied"] = True
            self._save_metadata(meta)

    def _cleanup_target(self, worktree_id: str, entry) -> Path:
        """Validate a (possibly tampered) metadata entry. Returns the resolved
        candidate directory, or raises PatchError("cleanup_failed")."""
        match = _WORKTREE_ID.match(str(worktree_id))
        if not match or not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise PatchError("cleanup_failed", "malformed worktree metadata")
        path = Path(entry["path"])
        if not path.is_absolute():
            raise PatchError("cleanup_failed", "worktree path is not absolute")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise PatchError("cleanup_failed", "worktree path cannot be resolved")
        # The recorded path itself must not be a link/junction alias.
        if os.path.normcase(str(resolved)) != os.path.normcase(os.path.abspath(str(path))):
            raise PatchError("cleanup_failed", "worktree path is a link or alias")
        if not path_strictly_contains(self.workspace, resolved):
            raise PatchError("cleanup_failed", "worktree path escapes the workspace")
        # Exactly <workspace>/<incident>/candidate-<id>: never the workspace root,
        # an incident directory or anything deeper/shallower.
        if (os.path.normcase(str(resolved.parent.parent)) != os.path.normcase(str(self.workspace))
                or os.path.normcase(resolved.name) != os.path.normcase(f"candidate-{match.group(1)}")):
            raise PatchError("cleanup_failed", "worktree path is not an application candidate directory")
        for label, root in (("target repository", entry.get("target_root")),
                            ("application repository", self._app_root)):
            if root and paths_overlap(resolved, Path(str(root)).resolve()):
                raise PatchError("cleanup_failed", f"refusing to remove a path overlapping the {label}")
        if not resolved.is_dir():
            raise PatchError("cleanup_failed", "worktree path is not a directory")
        return resolved

    def cleanup(self, worktree_id: str) -> dict:
        """Remove one application-owned worktree. The path comes from the
        metadata (never from a caller/model) and is re-validated as hostile
        input immediately before deletion."""
        entry = self._load_metadata().get(worktree_id)
        if entry is None:
            raise PatchError("unknown_worktree", f"no application-owned worktree {worktree_id!r}")
        if self._app_root is None:
            from patching.policy import application_root
            self._app_root = application_root()
        resolved = self._cleanup_target(worktree_id, entry)
        try:
            shutil.rmtree(resolved)
        except OSError as e:
            raise PatchError("cleanup_failed", f"cleanup failed: {e}")
        self._unregister(worktree_id)
        return {"worktree_id": worktree_id, "removed": str(resolved)}
