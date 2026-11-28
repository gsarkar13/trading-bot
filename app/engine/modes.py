"""Effective mode per strategy (brief sections 5.2 and invariants 10, 14).

The requested mode (strategies table) can only be LOWERED, never raised:
  1. the global ceiling (PAPER/SEMI/LIVE)
  2. real money (SEMI or LIVE) needs: the strategy's Phase 3 gate passed (or overridden),
     a valid data login, and real money armed for TODAY in THIS process (two-step)
  3. SEMI also needs `allow_semi` for the strategy (off for options by default)
  4. LIVE also needs: an `execution` instance, a passing static-IP check and a valid
     execution-broker login
Every lowering comes with a plain reason for the dashboard and the startup alert.
A position opened with real money is never managed as paper, and vice versa: that is
handled per position, not here (section 5.2.5).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.types import GateStatus, InstanceRole, Mode


@dataclass(frozen=True)
class ModeInputs:
    requested: Mode
    ceiling: Mode
    allow_semi: bool
    gate: GateStatus
    armed_today: bool
    data_token_ok: bool
    role: InstanceRole
    static_ip_ok: bool
    exec_token_ok: bool


@dataclass(frozen=True)
class Resolved:
    mode: Mode
    reasons: tuple[str, ...]


def resolve_mode(i: ModeInputs) -> Resolved:
    reasons: list[str] = []
    mode = Mode.lowest(i.requested, i.ceiling)
    if mode is not i.requested:
        reasons.append(f"global ceiling is {i.ceiling.value}")
    if mode.is_real_money:
        blockers = []
        if i.gate not in (GateStatus.PASSED, GateStatus.OVERRIDDEN):
            blockers.append(f"gate {i.gate.value}")
        if not i.armed_today:
            blockers.append("real money not armed today")
        if not i.data_token_ok:
            blockers.append("no data login today")
        if i.role is not InstanceRole.EXECUTION:
            blockers.append("this is a research instance (invariant 16)")
        if blockers:
            return Resolved(Mode.PAPER, tuple(reasons + blockers))
    if mode is Mode.LIVE:
        live_blockers = []
        if i.role is not InstanceRole.EXECUTION:
            live_blockers.append("this is a research instance")
        if not i.static_ip_ok:
            live_blockers.append("static-IP check not passed")
        if not i.exec_token_ok:
            live_blockers.append("no execution-broker login")
        if live_blockers:
            reasons += live_blockers
            mode = Mode.SEMI if i.allow_semi else Mode.PAPER
            if mode is Mode.PAPER and i.allow_semi is False:
                reasons.append("SEMI not allowed for this strategy")
    if mode is Mode.SEMI and not i.allow_semi:
        reasons.append("SEMI not allowed for this strategy")
        mode = Mode.PAPER
    return Resolved(mode, tuple(reasons))


def live_loss_action(segment_is_options: bool, semi_fallback: bool) -> str:
    """Invariant 14: what a LIVE strategy does when it loses the ability to place orders
    mid-session. Never a silent downgrade: no new entries + critical alert; open
    positions stay protected by their broker-side stops. Only a stock strategy with
    `semi_fallback: true` may continue with SEMI entries."""
    return "semi_entries" if (semi_fallback and not segment_is_options) else "no_new_entries"
