"""
Temporary, isolated fixture repositories for Stage 5 tests (never the Incigent repo).
Built with dulwich in a pytest tmp_path: a small service with a deliberate bug,
a test file, test config, a secret file and a committed history.
"""

import hashlib
import os
import sys
from pathlib import Path

from dulwich import porcelain

AUTH_PY = (b"def validate_token(token):\n"
           b"    if not token:\n"
           b"        return None\n"
           b"    session = lookup(token)\n"
           b"    return user_id  # BUG: user_id is undefined -> NameError -> HTTP 500\n")
TEST_AUTH_PY = (b"from src.auth import validate_token\n\n"
                b"def test_valid_token_returns_user():\n"
                b"    assert validate_token('abc') is not None\n")
IDENTITY = b"Dev <dev@example.com>"


def make_repo(base: Path, *, dirty: bool = False) -> Path:
    repo = base / "service-repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    porcelain.init(str(repo))
    (repo / "src" / "__init__.py").write_bytes(b"")
    (repo / "src" / "auth.py").write_bytes(b"def validate_token(token):\n    return lookup(token)\n")
    (repo / "tests" / "test_auth.py").write_bytes(TEST_AUTH_PY)
    (repo / "pytest.ini").write_bytes(b"[pytest]\ntestpaths = tests\n")
    (repo / ".env").write_bytes(b"API_KEY=sk-live-abcdefghijklmnopqrstuvwx\n")
    (repo / "deploy.pem").write_bytes(b"-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n")
    (repo / ".gitignore").write_bytes(b".env\n*.pem\n")
    tracked = ["src/__init__.py", "src/auth.py", "tests/test_auth.py", "pytest.ini", ".gitignore"]
    porcelain.add(str(repo), paths=[str(repo / p) for p in tracked])
    porcelain.commit(str(repo), message=b"initial service", author=IDENTITY, committer=IDENTITY)
    (repo / "src" / "auth.py").write_bytes(AUTH_PY)
    porcelain.add(str(repo), paths=[str(repo / "src" / "auth.py")])
    porcelain.commit(str(repo), message=b"refactor token validation", author=IDENTITY, committer=IDENTITY)
    if dirty:
        (repo / "src" / "auth.py").write_bytes(AUTH_PY + b"# work in progress\n")
    return repo


def tree_digest(root: Path) -> dict:
    """path -> sha256 of every file INCLUDING .git internals (byte-for-byte snapshot)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.is_symlink():
                out[str(p.relative_to(root))] = "link:" + os.readlink(p)
                continue
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def make_outside_link(link: Path, target_dir: Path) -> str:
    """Create a directory link inside a repo pointing outside it.
    Returns "junction" or "symlink", or raises OSError if neither is possible."""
    if sys.platform == "win32":
        import _winapi  # junctions need no special privilege on Windows
        _winapi.CreateJunction(str(target_dir), str(link))
        return "junction"
    os.symlink(target_dir, link, target_is_directory=True)
    return "symlink"
