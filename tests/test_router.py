import asyncio
from collections.abc import Sequence

import pytest

from app.adapters.base import BaseProvider, Capability
from app.adapters.router import CapabilityRouter, NoProviderError


class Fake(BaseProvider):
    def __init__(self, name: str, caps: Sequence[Capability], healthy: bool = True) -> None:
        self.name, self._caps, self.healthy = name, frozenset(caps), healthy

    def capabilities(self) -> frozenset[Capability]:
        return self._caps

    async def health(self) -> bool:
        return self.healthy


C = Capability


def _router(fy_ok: bool = True, ft_ok: bool = True) -> CapabilityRouter:
    fy = Fake("fyers", [C.HISTORICAL_CANDLES, C.INSTRUMENT_MASTER, C.EXECUTION], fy_ok)
    ft = Fake("flattrade", [C.INSTRUMENT_MASTER, C.EXECUTION], ft_ok)
    r = CapabilityRouter(
        {"fyers": fy, "flattrade": ft},
        {
            C.HISTORICAL_CANDLES: ["fyers"],
            C.INSTRUMENT_MASTER: ["fyers", "flattrade"],
            C.EXECUTION: ["flattrade", "fyers"],
        },
    )
    asyncio.run(r.refresh_health())
    return r


def test_data_fails_over_to_next_healthy() -> None:
    assert _router().get(C.INSTRUMENT_MASTER).name == "fyers"
    assert _router(fy_ok=False).get(C.INSTRUMENT_MASTER).name == "flattrade"


def test_execution_never_fails_over() -> None:
    assert _router().get(C.EXECUTION).name == "flattrade"
    with pytest.raises(NoProviderError, match="no failover"):
        _router(ft_ok=False).get(C.EXECUTION)


def test_all_unhealthy_raises() -> None:
    with pytest.raises(NoProviderError):
        _router(fy_ok=False).get(C.HISTORICAL_CANDLES)


def test_routing_to_unsupported_capability_rejected() -> None:
    ft = Fake("flattrade", [C.EXECUTION])
    with pytest.raises(ValueError, match="does not support"):
        CapabilityRouter({"flattrade": ft}, {C.OPTION_CHAIN: ["flattrade"]})
    with pytest.raises(ValueError, match="unknown provider"):
        CapabilityRouter({"flattrade": ft}, {C.EXECUTION: ["zerodha"]})
