"""A small harness that drives `TradingEngine` with scripted ticks (tests and chaos)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

from app.adapters.base import InstrumentInfo, TickData
from app.adapters.fyers.symbols import SymbolMap, parse_master
from app.brokers.paper import Depth, PaperBroker, PaperConfig
from app.config.loader import REPO_ROOT
from app.config.schema import PdhPdlParams, PlatformConfig, StrategyEntry
from app.core.types import InstrumentKey, OptionType, Segment
from app.db.models import StrategyRow
from app.db.seed import sync_strategies_table
from app.engine.alerts import AlertBus
from app.engine.engine import Health, Slot, TradingEngine
from app.research.costs import ChargesTable
from app.strategies.base import (
    DataNeeds,
    ExitDecision,
    MarketView,
    OpenPosition,
    Signal,
    StrategyPlugin,
    WarmupRequirement,
)

D = Decimal
FIX = Path(__file__).parent / "fixtures" / "fyers"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAY = dt.date(2026, 12, 2)  # a Wednesday; nearest NIFTY weekly is Tue 8 Dec
OPT = InstrumentKey.option("NIFTY", dt.date(2026, 12, 8), 24000, OptionType.CE)


def ist(h: int, m: int, s: float = 0, d: dt.date = DAY) -> dt.datetime:
    return dt.datetime.combine(d, dt.time(h, m), IST) + dt.timedelta(seconds=s)


class Clock:
    def __init__(self, t: dt.datetime) -> None:
        self.t = t

    def __call__(self) -> dt.datetime:
        return self.t


class ScriptedStrategy(StrategyPlugin):
    """Emits the signals it is told to, at the candles it is told to."""

    id: ClassVar[str] = "pdh_pdl_breakout"
    segment: ClassVar[Segment] = Segment.OPTIONS
    params_model: ClassVar[type[BaseModel]] = PdhPdlParams

    def __init__(self, params: BaseModel, config_hash: str) -> None:
        super().__init__(params, config_hash)
        self.script: dict[dt.datetime, tuple[str, float, float, float | None]] = {}
        self.opened: list[Signal] = []
        self.exit_action: str = "HOLD"

    @classmethod
    def data_needs(cls, params: BaseModel) -> DataNeeds:
        return DataNeeds(timeframes=("1m",))

    @classmethod
    def warmup(cls, params: BaseModel) -> WarmupRequirement:
        return WarmupRequirement(bars={"1m": 0})

    def on_candle(self, timeframe: str, view: MarketView) -> list[Signal]:
        s = self.script.get(view.bar.ts)
        if s is None:
            return []
        direction, ref, stop, target = s
        return [
            Signal(
                strategy_id=self.id,
                underlying=view.underlying,
                direction=direction,  # type: ignore[arg-type]
                at=view.now,
                reference_price=D(str(ref)),
                stop_underlying=D(str(stop)),
                target_underlying=None if target is None else D(str(target)),
                checklist={"scripted": True},
                config_hash=self.config_hash,
            )
        ]

    def on_trade_opened(self, signal: Signal) -> None:
        self.opened.append(signal)

    def exit_logic(self, position: OpenPosition, view: MarketView) -> ExitDecision:
        return ExitDecision(action=self.exit_action)  # type: ignore[arg-type]


def activate(db: Engine, sid: str = "pdh_pdl_breakout") -> None:
    with Session(db) as s:
        sync_strategies_table(s, ist(8, 0))
        s.execute(update(StrategyRow).where(StrategyRow.id == sid).values(active=True))
        s.commit()


def smap() -> SymbolMap:
    return SymbolMap(parse_master((FIX / "NSE_FO_sample.csv").read_text()))


class Harness:
    def __init__(
        self,
        db: Engine,
        state_dir: Path,
        clock: Clock | None = None,
        cfg: PlatformConfig | None = None,
        process_id: str = "proc-1",
        quote: Depth | None = None,
        strategy: ScriptedStrategy | None = None,
        paper_cfg: Callable[[], PaperConfig] | None = None,
        live: PaperBroker | None = None,
    ) -> None:
        self.db, self.state_dir = db, state_dir
        self.clock = clock or Clock(ist(9, 59))
        self.cfg = cfg or PlatformConfig(
            strategies=(StrategyEntry(id="pdh_pdl_breakout", active=True),)
        )
        self.strategy = strategy or ScriptedStrategy(PdhPdlParams(), "hash-test")
        self.quote = quote or Depth(D("120.00"), D("120.50"), 650, 650, D("120.20"), self.clock())
        self.alerts = AlertBus(db, self.cfg.instance.instance_id, self.clock)
        self.health = Health(data_login=True, clock_drift_ms=0.0)
        mk = paper_cfg or (lambda: PaperConfig(latency_s=0.0))

        def paper(book: str, inst: dict[InstrumentKey, InstrumentInfo]) -> PaperBroker:
            safe = book.replace(":", "_")
            return PaperBroker(inst, self.clock, mk(), state_path=state_dir / f"{safe}.json")

        async def fetch(key: InstrumentKey) -> Depth | None:
            q = self.quote
            return Depth(q.bid, q.ask, q.bid_qty, q.ask_qty, q.ltp, self.clock())

        self.engine = TradingEngine(
            self.cfg,
            db,
            smap(),
            self.clock,
            [Slot("pdh_pdl_breakout", self.strategy, self.cfg.strategies[0], 0)],
            self.alerts,
            ChargesTable(REPO_ROOT / "config" / "charges.yaml"),
            process_id,
            paper,
            live=live,
            history=None,
            quote_fetch=fetch,
            health=self.health,
        )

    def at(self, t: dt.datetime) -> None:
        self.clock.t = t

    async def index(self, px: float, t: dt.datetime | None = None, u: str = "NIFTY") -> None:
        if t is not None:
            self.at(t)
        await self.engine.on_tick(
            TickData(
                key=InstrumentKey.index(u),
                exchange_ts=self.clock(),
                received_ts=self.clock(),
                ltp=D(str(px)),
            )
        )

    async def minute(self, h: int, m: int, px: float, u: str = "NIFTY") -> None:
        """A flat candle at `px` for h:m, closed by the timer 2.5 s after the minute."""
        await self.index(px, ist(h, m, 0), u)
        await self.index(px, ist(h, m, 58), u)
        self.at(ist(h, m + 1 if m < 59 else 0, 2.5) if m < 59 else ist(h + 1, 0, 2.5))
        await self.engine.on_timer()

    async def option(self, bid: str, ask: str, key: InstrumentKey = OPT) -> None:
        self.quote = Depth(D(bid), D(ask), 650, 650, D(bid), self.clock())
        await self.engine.on_option_depth(key, self.quote)
