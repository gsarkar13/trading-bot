"""Invariant 18: no secrets in the repository; the detector itself works."""

import subprocess

from app.config.loader import REPO_ROOT
from app.core.scrub import find_secrets, redact

ALLOW = {".env.example"}  # placeholders only; still scanned below for real values


def _repo_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [f for f in out.splitlines() if f]


def test_no_token_like_strings_in_repo() -> None:
    offenders = {}
    for rel in _repo_files():
        p = REPO_ROOT / rel
        if not p.is_file() or p.suffix in {".png", ".ico", ".woff2"}:
            continue
        hits = find_secrets(p.read_text(encoding="utf-8", errors="ignore"))
        if hits:
            offenders[rel] = hits
    assert not offenders, offenders
    assert ALLOW  # .env.example is scanned too, and must contain no values


def test_env_and_vault_are_ignored() -> None:
    files = set(_repo_files())
    assert ".env" not in files
    assert not any(f.endswith((".db", ".vault", ".parquet")) for f in files)
    assert not any(f.startswith(("data/", "logs/")) for f in files)


def test_detector_catches_known_shapes() -> None:
    # Built at runtime so this file never contains a real-looking secret.
    jwt = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 10
    tg = "1234567890:" + "A" * 35
    assigned = "access_token = " + "Z9" * 15
    for sample in (jwt, tg, "sk-ant-" + "x" * 30, "AIza" + "q" * 35, assigned):
        assert find_secrets(sample), sample
        assert "[REDACTED]" in redact(sample)
    assert not find_secrets("config_hash = 0123456789abcdef")  # 16 chars: not flagged
