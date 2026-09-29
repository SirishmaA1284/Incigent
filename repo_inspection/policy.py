"""
repo_inspection/policy.py
Limits, path validation and the sensitive-file policy for read-only repository
inspection. Everything here is pure: no filesystem writes, no commands.
"""

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


class InspectionError(Exception):
    """A request was refused. `code` is stable and machine-readable."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class InspectionLimits:
    max_file_bytes: int = 256 * 1024      # larger files are not read at all
    max_lines: int = 400                  # lines returned per read
    max_list_entries: int = 500           # entries returned per listing
    max_output_chars: int = 60_000        # total evidence text per collection
    max_path_length: int = 260
    max_commits: int = 20
    max_diff_bytes: int = 64 * 1024
    max_tracked_files: int = 20_000   # beyond this, working-tree status/diff is
                                      # refused (structured "limited" result):
                                      # dulwich status hashes every tracked file

    @classmethod
    def from_env(cls) -> "InspectionLimits":
        def env_int(name, default):
            try:
                value = int(os.getenv(name, ""))
                return value if value > 0 else default
            except ValueError:
                return default
        d = cls()
        return cls(
            max_file_bytes=env_int("REPO_INSPECT_MAX_FILE_BYTES", d.max_file_bytes),
            max_lines=env_int("REPO_INSPECT_MAX_LINES", d.max_lines),
            max_list_entries=env_int("REPO_INSPECT_MAX_LIST_ENTRIES", d.max_list_entries),
            max_output_chars=env_int("REPO_INSPECT_MAX_OUTPUT_CHARS", d.max_output_chars),
            max_tracked_files=env_int("REPO_INSPECT_MAX_TRACKED_FILES", d.max_tracked_files),
        )

    def to_dict(self) -> dict:
        return asdict(self)


# ── Sensitive files: denied by default (never read, never diffed) ─────────────
_SAFE_ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template", ".env.dist"}
_SENSITIVE_NAMES = {
    ".env", ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".htpasswd",
    "credentials", "credentials.json", "credentials.yml", "credentials.yaml",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "known_hosts", "authorized_keys",
    "secrets.json", "secrets.yml", "secrets.yaml", "secrets.toml", ".secrets",
    "token", "token.txt", "token.json", ".token", "access_token", "refresh_token",
    "kubeconfig", "service-account.json", "service_account.json",
}
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".ppk", ".asc", ".gpg")
_SENSITIVE_DIRS = {".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker", ".gcloud"}
_SENSITIVE_PATTERNS = [
    re.compile(r"^\.env\..+$"),                         # .env.local, .env.production ...
    re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)(_\w+)?$"), # private SSH keys (not *.pub)
    re.compile(r".*service[-_]?account.*\.json$"),
    re.compile(r".*credential.*\.(json|ya?ml|toml|ini|txt|cfg|conf|xml|properties|csv)$", re.I),
    re.compile(r".*secret.*\.(json|ya?ml|toml|ini|txt|env)$", re.I),
]


def is_sensitive(rel_path: str) -> bool:
    """Deny-by-default policy for obvious secret and credential files."""
    parts = PurePosixPath(rel_path.replace("\\", "/")).parts
    if not parts:
        return False
    name = parts[-1].lower()
    if name in _SAFE_ENV_EXAMPLES:
        return False
    if any(p.lower() in _SENSITIVE_DIRS for p in parts[:-1]):
        return True
    if name in _SENSITIVE_NAMES or name.endswith(_SENSITIVE_SUFFIXES):
        return True
    return any(p.match(name) for p in _SENSITIVE_PATTERNS)


# ── Path validation ───────────────────────────────────────────────────────────
def validate_relative_path(rel_path, limits: InspectionLimits) -> str:
    """Syntactic checks on a caller-supplied relative path. Returns it in POSIX form.
    This never touches the filesystem; containment is checked after resolution."""
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise InspectionError("invalid_path", "path must be a non-empty string")
    if len(rel_path) > limits.max_path_length or "\x00" in rel_path:
        raise InspectionError("invalid_path", "path too long or contains NUL")
    posix = rel_path.replace("\\", "/")
    win = PureWindowsPath(rel_path)
    if posix.startswith("/") or win.drive or win.root or PurePosixPath(posix).is_absolute():
        raise InspectionError("absolute_path", "absolute paths are not allowed")
    parts = PurePosixPath(posix).parts
    if any(p == ".." for p in parts):
        raise InspectionError("path_traversal", "'..' is not allowed")
    if any(p.lower() == ".git" for p in parts):
        raise InspectionError("git_internals", "use the git inspection operations instead")
    clean = "/".join(p for p in parts if p not in ("", "."))
    return clean or "."


def resolve_inside(root: Path, rel_path: str, limits: InspectionLimits) -> Path:
    """Resolve rel_path under root, following links, and require the FINAL
    resolved path to stay inside root (defeats symlink/junction escapes)."""
    clean = validate_relative_path(rel_path, limits)
    candidate = root.joinpath(*PurePosixPath(clean).parts) if clean != "." else root
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise InspectionError("not_found", "no such file or directory")
    except (OSError, RuntimeError):
        raise InspectionError("unresolvable", "path could not be resolved")
    if not is_within(root, resolved):
        raise InspectionError("outside_root", "path resolves outside the repository")
    return resolved


def safe_walk(root: Path, start: Path):
    """Yield (relative_posix_path, real_path) for files under start, never leaving
    root: .git is skipped and every directory/file's REAL location is checked,
    because on Windows os.walk does not treat junctions as links. Returns the
    count of skipped out-of-root entries via the generator's return value."""
    skipped = 0
    visited = {start.resolve()}   # real directories already walked: breaks link/junction loops
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        here = Path(dirpath)
        candidates = []
        for d in sorted(dirnames):
            if d == ".git":
                continue
            try:
                real_dir = (here / d).resolve()
                inside = is_within(root, real_dir)
            except OSError:
                inside, real_dir = False, None
            if not inside:
                skipped += 1
                continue
            is_alias = os.path.normcase(str(real_dir)) != os.path.normcase(str((here / d).absolute()))
            candidates.append((is_alias, d, real_dir))
        kept = []
        for _, d, real_dir in sorted(candidates):          # real directories before link aliases
            if real_dir not in visited:
                visited.add(real_dir)
                kept.append(d)
        dirnames[:] = sorted(kept)
        for name in sorted(filenames):
            if name == ".git":          # linked-worktree pointer: git internals
                continue
            full = here / name
            try:
                real = full.resolve()
            except OSError:
                continue
            if not is_within(root, real):
                skipped += 1
                continue
            yield full.relative_to(root).as_posix(), real
    return skipped


def is_within(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
