export type Mode = "PAPER" | "SEMI" | "LIVE";

export interface Position {
  uid: string; book: string; mode: Mode; strategy: string; key: string;
  state: string; qty: number; stop: number; target: number | null;
}
export interface SemiTask {
  uid: string; symbol: string; qty: number; limit: string; status: string;
  stop: string[] | null; message: string; created: string;
}
export interface Breaker { tripped: boolean; reasons: string[]; today_realized: string; trades_today: number }
export interface Pnl { net: string; trades: number; win_rate: number | null }
export interface Alert { at: string; level: string; category: string; message: string }
export interface Status {
  now_ist: string;
  engine: { halted: string | null; signals_today: number };
  armed: boolean; ceiling: Mode; config_ceiling: Mode;
  host: { label: string; role: string; instance_id: string; static_ip_ok: boolean;
          clock_drift_ms: number | null; live_possible: boolean; live_blockers: string[] };
  feeds: Record<string, unknown>;
  last_tick: Record<string, string | null>;
  spot: Record<string, number>; vix: number | null;
  order_rate: Record<string, { last_second: number; last_minute: number;
                               per_second_limit: number; per_minute_limit: number }>;
  pnl: { paper: Pnl; real: Pnl; shadow: Pnl };
  breakers: { PAPER: Breaker; REAL: Breaker };
  positions: Position[]; semi_tasks: SemiTask[]; alerts: Alert[];
}
export interface StrategyRow {
  id: string; priority: number; name: string; segment: string; stage: string;
  code_status: string; active: boolean; requested_mode: Mode; effective_mode: Mode;
  effective_reasons: string[]; gate_status: string; gate_passed_on: string | null;
  notes: string; version: number; config_hash: string | null; warm: string | null;
  exposed: boolean;
}
export interface SignalOut {
  uid: string; at: string; strategy: string; underlying: string; direction: string;
  outcome: string; checklist: Record<string, unknown>; inputs: Record<string, unknown>;
  ai: { approve: boolean; confidence: number; reason: string } | null;
}
export interface TradeOut {
  id: number; strategy: string | null; book: string; instrument: string; entry_at: string;
  exit_at: string; qty: number; entry: string; exit: string; gross: string;
  charges: Record<string, string>; charges_total: string; net: string;
  compliance: number | null; overrides: Record<string, unknown>[]; self_reported: boolean;
}
export interface BookStat {
  book: string; trades: number; win_rate: number; expectancy: string;
  profit_factor: number | null; max_drawdown: string; net_pnl: string; charges: string;
}
export interface EventOut { at: string; category: string; severity: string; message: string }
export interface JournalOut {
  date: string; stats: Record<string, unknown>; ai_review: string | null;
  ai_provider: string | null; notes: string | null;
}
