"""
patching/verify.py
Candidate verification for Stage 6.

Runtime test execution is NOT provided: repository tests are potentially
untrusted code and there is no sandbox, so nothing is ever executed. The only
verifier is STRUCTURAL and runs in-process without importing or executing
repository code: the patched file exists, the replacement is present, the file
is not binary, and (for Python) the file still parses via ast.parse — which
parses but never executes. Tests are always reported as not_run.
"""

import ast
from dataclasses import dataclass, field


@dataclass
class VerificationResult:
    status: str                 # "passed" | "failed" | "not_run"
    mode: str = "structural"    # what was actually checked
    checks: list = field(default_factory=list)
    tests: dict = field(default_factory=dict)   # always not_run: no sandbox
    reason: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "mode": self.mode, "checks": list(self.checks),
                "tests": dict(self.tests), "reason": self.reason}


class StructuralVerifier:
    """Safe, in-process verification of the patched candidate worktree."""

    def verify(self, worktree, intent, *, limits, diff_summary=None) -> VerificationResult:
        """diff_summary: the applied change measured against the PINNED BASE
        (added/removed lines, diff bytes). Size is judged on that delta, never
        on the absolute length of the file."""
        checks = []
        file_path = worktree.root / intent.file
        try:
            raw = file_path.read_bytes()
        except OSError as e:
            return VerificationResult("failed", reason=f"patched file unreadable: {e}")
        checks.append("patched file exists")
        if b"\x00" in raw[:8192]:
            return VerificationResult("failed", checks=checks, reason="patched file is binary")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return VerificationResult("failed", checks=checks, reason="patched file is not valid UTF-8")
        checks.append("patched file is UTF-8 text")
        if intent.replacement_content not in text:
            return VerificationResult("failed", checks=checks, reason="replacement content missing after apply")
        checks.append("replacement content present")
        if diff_summary is not None:
            if (diff_summary.get("added", 0) > limits.max_lines_added
                    or diff_summary.get("removed", 0) > limits.max_lines_removed
                    or diff_summary.get("diff_bytes", 0) > limits.max_diff_bytes):
                return VerificationResult("failed", checks=checks,
                                          reason="change relative to the pinned base exceeds the patch limits")
            checks.append("change relative to the pinned base is within limits")
        if file_path.suffix == ".py":
            try:
                ast.parse(text, filename=intent.file)
                checks.append("python file still parses (ast.parse, no execution)")
            except SyntaxError as e:
                return VerificationResult("failed", checks=checks,
                                          reason=f"patched python file does not parse: {e}")
        return VerificationResult("passed", checks=checks, tests={
            "status": "not_run",
            "reason": "no sandboxed test runner exists in Stage 6; repository tests are not executed"})


def build_verifier(config):
    """None when verification is disabled in configuration."""
    if config is None or getattr(config, "verify", "structural") != "structural":
        return None
    return StructuralVerifier()