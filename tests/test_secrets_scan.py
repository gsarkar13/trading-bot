"""Invariant 18: no secrets in the repository; the detector itself works."""

import subprocess

from app.config.loader import REPO_ROOT
from app.core.scrub import find_secrets, redact


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
    assert not offenders, offenders  # .env.example is scanned too


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
    env_lines = [
        "ALGO_FYERS_APP_SECRET=" + "a1" * 12,
        "api_secret=" + "b2" * 12,
        "ALGO_TELEGRAM_BOT_TOKEN=" + "c3" * 12,
        '{"jKey": "' + "d4" * 12 + '"}',
        "appIdHash: " + "e5" * 12,
    ]
    sha256_hex = "f0" * 32
    for sample in (
        jwt,
        tg,
        "sk-ant-" + "x" * 30,
        "AIza" + "q" * 35,
        assigned,
        sha256_hex,
        *env_lines,
    ):
        assert find_secrets(sample), sample
        assert "[REDACTED]" in redact(sample)
    # Things that must NOT be flagged: short hashes, empty example values, type hints.
    assert not find_secrets("config_hash = 0123456789abcdef")
    assert not find_secrets("ALGO_FYERS_APP_SECRET=")
    assert not find_secrets("fyers_app_secret: SecretStr | None = None")


def test_no_source_file_is_git_ignored() -> None:
    """A broad ignore rule once hid app/data/ from git (CI failure on PR #2)."""
    sources = [
        str(p.relative_to(REPO_ROOT))
        for d in ("app", "tests", "alembic", "config")
        for p in (REPO_ROOT / d).rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.suffix in {".py", ".yaml", ".csv", ".md", ".mako"}
    ]
    ignored = subprocess.run(  # noqa: S603
        ["git", "check-ignore", "--no-index", *sources],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.split()
    allowed = {"config/host.yaml"}  # per-host, deliberately untracked
    assert [f for f in ignored if f not in allowed] == []
