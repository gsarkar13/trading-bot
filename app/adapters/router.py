"""Capability router (brief section 14.2).

Assigns one provider per capability from the configured preference list and fails over
to the next healthy provider. Execution is special: it NEVER fails over automatically,
because switching execution broker mid-session would orphan open orders and positions.
A failed execution provider means "no new entries" (invariant 14), not "try the other one".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.adapters.base import BaseProvider, Capability


class NoProviderError(RuntimeError):
    pass


class CapabilityRouter:
    def __init__(
        self,
        providers: Mapping[str, BaseProvider],
        routing: Mapping[Capability, Sequence[str]],
    ) -> None:
        self._providers = dict(providers)
        self._routing = {cap: tuple(names) for cap, names in routing.items()}
        for cap, names in self._routing.items():
            for n in names:
                if n not in self._providers:
                    raise ValueError(f"routing for {cap} names unknown provider {n!r}")
                if cap not in self._providers[n].capabilities():
                    raise ValueError(f"provider {n!r} does not support {cap}")
        self._unhealthy: set[str] = set()

    def mark_unhealthy(self, name: str) -> None:
        self._unhealthy.add(name)

    def mark_healthy(self, name: str) -> None:
        self._unhealthy.discard(name)

    async def refresh_health(self) -> None:
        for name, p in self._providers.items():
            if await p.health():
                self.mark_healthy(name)
            else:
                self.mark_unhealthy(name)

    def get(self, cap: Capability) -> BaseProvider:
        names = self._routing.get(cap, ())
        if not names:
            raise NoProviderError(f"no provider configured for {cap}")
        if cap is Capability.EXECUTION:
            primary = names[0]
            if primary in self._unhealthy:
                raise NoProviderError(f"execution provider {primary!r} unhealthy; no failover")
            return self._providers[primary]
        for n in names:
            if n not in self._unhealthy:
                return self._providers[n]
        raise NoProviderError(f"all providers for {cap} are unhealthy: {list(names)}")
