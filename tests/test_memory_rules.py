"""Proposal P7 / D10: live processes never import pandas (memory budget, section 2.3)."""

import subprocess
import sys

LIVE_MODULES = [
    "app.services.recorder",
    "app.adapters.fyers.ws",
    "app.adapters.fyers.client",
    "app.strategies.plugins",
    "app.tools.login",
    "app.tools.backfill",
]


def test_live_processes_do_not_import_pandas() -> None:
    code = (
        "import sys, importlib\n"
        f"for m in {LIVE_MODULES!r}: importlib.import_module(m)\n"
        "import fyers_apiv3.FyersWebsocket.data_ws\n"
        "bad = [m for m in ('pandas', 'numpy.core._multiarray_umath') if m in sys.modules]\n"
        "print(','.join(bad))"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert "pandas" not in out, out
