#!/usr/bin/env python3
"""Privacy guard for a public repository that handles personal activity data.

Checks every tracked or untracked-but-not-ignored file (and, with ``--history``, everything ever
added in the git history) for:

* forbidden file types (FIT, GPX, TCX, archives, databases, ``activities.csv``, export CSVs);
* GPS coordinates inside the Germany/Alps bounding box (allowed only in test files that carry the
  marker ``SYNTHETIC``);
* secret-like strings (tokens, keys; modelled on the usual gitleaks rules);
* e-mail addresses and phone numbers (except obviously fictional ones);
* terms from the optional ``DENYLIST`` environment variable (comma or newline separated). Matches
  are reported without the term, so a CI log never leaks it;
* test fixtures without the marker ``SYNTHETIC``.

Exit status 1 if anything is found. Only the standard library is used.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MARKER = "SYNTHETIC"
MAX_TEXT_BYTES = 2_000_000

FORBIDDEN_SUFFIXES = {
    ".fit",
    ".gpx",
    ".tcx",
    ".gz",
    ".tgz",
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".bz2",
    ".xz",
    ".db",
    ".sqlite",
    ".sqlite3",
}
# Text fixtures may exist under this folder if they carry the marker (checked below).
SYNTHETIC_FIXTURE_DIR = "tests/fixtures/synthetic/"
SYNTHETIC_ALLOWED_SUFFIXES = {".gpx", ".tcx"}
SKIP_DIRS = {".git", "__pycache__", ".ruff_cache", ".pytest_cache", ".venv", "venv", "node_modules"}

# Bounding box around Germany and the Alps: (lat_min, lat_max, lon_min, lon_max)
BBOX = (43, 56, 5, 18)

PAIR_RE = re.compile(r"(-?\d{1,3}\.\d{3,})\s*[,;/ ]\s*(-?\d{1,3}\.\d{3,})")
KEYED_LAT_RE = re.compile(r"""lat(?:itude)?["']?\s*[:=]\s*["']?(-?\d{1,3}\.\d+)""", re.I)
KEYED_LON_RE = re.compile(r"""lo?n(?:g|gitude)?["']?\s*[:=]\s*["']?(-?\d{1,3}\.\d+)""", re.I)

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "generic-secret-assignment",
        re.compile(
            r"""(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|client[_-]?secret)\b["']?\s*[:=]\s*"""
            r"""["'][A-Za-z0-9/+_\-=]{20,}["']"""
        ),
    ),
    (
        "url-with-credentials",
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@[^\s/]+"),
    ),
)

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
ALLOWED_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "users.noreply.github.com",
    "noreply.github.com",
    "anthropic.com",
}
PHONE_RES = (
    re.compile(r"(?<![\w.])\+\d[\d\s().-]{8,}\d"),
    re.compile(r"(?<![\w.])00\d{2}[\d\s/.-]{8,}\d"),
    re.compile(r"(?<![\w.\d-])0\d{2,5}[\s/-]\d{4,}"),
)
FICTIONAL_PHONE_PARTS = ("1234567", "5550", "0000000", "9999999")


@dataclass(frozen=True)
class Violation:
    """One finding. ``detail`` never contains the matched secret or denylist term."""

    path: str
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        """Format as ``path:line: [rule] detail``."""
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: [{self.rule}] {self.detail}"


def parse_denylist(value: str | None) -> list[str]:
    """Split the ``DENYLIST`` value into lower-case terms of at least three characters."""
    if not value:
        return []
    terms = re.split(r"[,\n]", value)
    return [t.strip().lower() for t in terms if len(t.strip()) >= 3]


def _in_bbox(a: float, b: float) -> bool:
    lat_min, lat_max, lon_min, lon_max = BBOX
    return any(
        lat_min <= lat <= lat_max and lon_min <= lon <= lon_max for lat, lon in ((a, b), (b, a))
    )


def _is_test_file(path: str) -> bool:
    return path.replace("\\", "/").startswith("tests/")


def forbidden_name(path: str) -> str | None:
    """Return a reason if the file name alone is not allowed in the repository."""
    posix = path.replace("\\", "/")
    name = posix.rsplit("/", 1)[-1].lower()
    suffix = Path(name).suffix
    if suffix in FORBIDDEN_SUFFIXES and not (
        posix.startswith(SYNTHETIC_FIXTURE_DIR) and suffix in SYNTHETIC_ALLOWED_SUFFIXES
    ):
        return f"file type {suffix} is not allowed (activity data or archive)"
    if name == "activities.csv" or (suffix == ".csv" and "export" in name):
        return "activity export CSV is not allowed"
    return None


def scan_text(
    path: str, text: str, denylist: list[str] | None = None, *, fixture_rule: bool = True
) -> list[Violation]:
    """Scan the text content of one file (``fixture_rule`` off for partial history diffs)."""
    found: list[Violation] = []
    posix = path.replace("\\", "/")
    has_marker = MARKER in text
    coordinates_ok = has_marker and _is_test_file(posix)
    if fixture_rule and posix.startswith("tests/fixtures/") and not has_marker:
        found.append(Violation(path, 0, "fixture-marker", f"fixture file lacks marker {MARKER}"))
    lowered_deny = denylist or []
    for number, line in enumerate(text.splitlines(), start=1):
        if not coordinates_ok:
            found += _coordinate_hits(path, number, line)
        for rule, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                found.append(Violation(path, number, "secret", f"matches rule {rule}"))
        for match in EMAIL_RE.finditer(line):
            domain = match.group(0).rsplit("@", 1)[1].lower()
            if domain not in ALLOWED_EMAIL_DOMAINS:
                found.append(Violation(path, number, "email", "e-mail address"))
        for pattern in PHONE_RES:
            for match in pattern.finditer(line):
                digits = re.sub(r"\D", "", match.group(0))
                if len(digits) >= 9 and not any(p in digits for p in FICTIONAL_PHONE_PARTS):
                    found.append(Violation(path, number, "phone", "phone number"))
        low = line.lower()
        for index, term in enumerate(lowered_deny, start=1):
            if term in low:
                found.append(Violation(path, number, "denylist", f"denylist entry #{index}"))
    return found


def _coordinate_hits(path: str, number: int, line: str) -> list[Violation]:
    hits: list[Violation] = []
    for match in PAIR_RE.finditer(line):
        if _in_bbox(float(match.group(1)), float(match.group(2))):
            hits.append(Violation(path, number, "coordinates", "position inside the DE/Alps box"))
    lat, lon = KEYED_LAT_RE.search(line), KEYED_LON_RE.search(line)
    if lat and lon and _in_bbox(float(lat.group(1)), float(lon.group(1))):
        hits.append(Violation(path, number, "coordinates", "lat/lon inside the DE/Alps box"))
    return hits


def list_files(root: Path) -> list[str]:
    """Return repository-relative file paths: git's view if available, else a directory walk."""
    if (root / ".git").exists():
        try:
            out = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                ],
                check=True,
                capture_output=True,
            ).stdout
            return sorted(p for p in out.decode().split("\0") if p and (root / p).is_file())
        except (OSError, subprocess.CalledProcessError):
            pass
    paths: list[str] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        paths += [str((Path(current) / f).relative_to(root)) for f in files]
    return sorted(paths)


def scan_root(root: Path, denylist: list[str] | None = None) -> list[Violation]:
    """Scan all files below ``root``."""
    found: list[Violation] = []
    for path in list_files(root):
        reason = forbidden_name(path)
        if reason:
            found.append(Violation(path, 0, "file-type", reason))
        deny_name = [t for t in (denylist or []) if t in path.lower()]
        if deny_name:
            found.append(Violation(path, 0, "denylist", "denylist entry in file name"))
        full = root / path
        try:
            if full.stat().st_size > MAX_TEXT_BYTES:
                continue
            data = full.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue  # binary: judged by its name only
        found += scan_text(path, data.decode("utf-8", "replace"), denylist)
    return found


def scan_history(root: Path, denylist: list[str] | None = None) -> list[Violation]:
    """Scan everything ever added in the git history (added lines per commit and file)."""
    try:
        log = subprocess.run(
            ["git", "-C", str(root), "log", "--all", "--no-color", "-p", "-U0", "--format=%x01%H"],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError):
        return [Violation("(history)", 0, "history", "git history could not be read")]
    found: list[Violation] = []
    for block in log.split("\x01")[1:]:
        commit, _, diff = block.partition("\n")
        files: dict[str, list[str]] = {}
        current = ""
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                current = line[6:]
                files.setdefault(current, [])
            elif line.startswith("+") and not line.startswith("+++") and current:
                files[current].append(line[1:])
        for name, added in files.items():
            reason = forbidden_name(name)
            if reason:
                found.append(Violation(f"{name} @ {commit[:8]}", 0, "file-type", reason))
            for hit in scan_text(name, "\n".join(added), denylist, fixture_rule=False):
                found.append(
                    Violation(f"{hit.path} @ {commit[:8]}", hit.line, hit.rule, hit.detail)
                )
    return found


def main(argv: list[str] | None = None) -> int:
    """Run the guard; return the process exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--history", action="store_true", help="also scan the git history")
    args = parser.parse_args(argv)
    denylist = parse_denylist(os.environ.get("DENYLIST"))
    findings = scan_root(args.root, denylist)
    if args.history:
        findings += scan_history(args.root, denylist)
    for finding in findings:
        print(finding)
    status = "FAILED" if findings else "ok"
    print(
        f"privacy guard: {status} ({len(findings)} finding(s), denylist entries: {len(denylist)})"
    )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
