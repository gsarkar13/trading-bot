import { useEffect, useState } from "react";
import { get, inr, post } from "./api";
import type { BookStat, EventOut, JournalOut, Mode, StrategyRow, TradeOut } from "./types";

type Err = (m: string) => void;

function useData<T>(path: string, every = 15_000): [T | null, () => void] {
  const [d, setD] = useState<T | null>(null);
  const load = () => get<T>(path).then(setD).catch(() => undefined);
  useEffect(() => {
    load();
    const t = setInterval(load, every);
    return () => clearInterval(t);
  }, [path]);
  return [d, load];
}

export function Strategies({ onError }: { onError: Err }) {
  const [rows, reload] = useData<StrategyRow[]>("/api/strategies");
  const act = (p: Promise<unknown>) => p.then(reload).catch((e: Error) => onError(e.message));
  return (
    <div className="card overflow-x-auto">
      <h2 className="mb-2 text-sm font-semibold">Strategies</h2>
      <table className="tbl">
        <thead>
          <tr><th>#</th><th>strategy</th><th>code</th><th>on</th><th>requested</th><th>effective</th><th>gate</th><th>warm-up</th><th></th></tr>
        </thead>
        <tbody>
          {(rows ?? []).map((r) => (
            <tr key={r.id}>
              <td>{r.priority}</td>
              <td title={r.notes}>{r.name}<div className="text-slate-500">{r.id} · v{r.version}</div></td>
              <td>{r.code_status}</td>
              <td>
                <input type="checkbox" checked={r.active} disabled={r.code_status !== "implemented"}
                  onChange={(e) => act(post(`/api/strategies/${r.id}/active`, { active: e.target.checked }))} />
              </td>
              <td>
                <select className="inp" value={r.requested_mode} disabled={r.code_status !== "implemented"}
                  onChange={(e) => act(post(`/api/strategies/${r.id}/mode`, { mode: e.target.value as Mode }))}>
                  {["PAPER", "SEMI", "LIVE"].map((m) => <option key={m}>{m}</option>)}
                </select>
              </td>
              <td title={r.effective_reasons.join("; ")}>
                {r.effective_mode}
                {r.effective_reasons.length > 0 && <div className="text-slate-500">{r.effective_reasons[0]}</div>}
              </td>
              <td>{r.gate_status}{r.gate_passed_on && ` (${r.gate_passed_on})`}</td>
              <td>{r.warm ?? "-"}</td>
              <td className="whitespace-nowrap">
                <button className="btn" onClick={() => act(post(`/api/strategies/${r.id}/move`, { up: true }))}>↑</button>{" "}
                <button className="btn" onClick={() => act(post(`/api/strategies/${r.id}/move`, { up: false }))}>↓</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const CATS = ["", "SIGNAL", "OI_FILTER", "AI_CHECK", "ORDER", "TRADE", "RISK", "LOGIN", "HOST", "DATA", "RECONCILE", "MODE", "ENGINE", "ERROR"];

export function Events() {
  const [cat, setCat] = useState("");
  const [rows] = useData<EventOut[]>(`/api/events?category=${cat}&limit=200`, 5_000);
  return (
    <div className="card">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="text-sm font-semibold">Events</h2>
        <select className="inp" value={cat} onChange={(e) => setCat(e.target.value)}>
          {CATS.map((c) => <option key={c} value={c}>{c || "all"}</option>)}
        </select>
      </div>
      <div className="max-h-72 overflow-y-auto font-mono text-xs">
        {(rows ?? []).map((e, i) => (
          <div key={i} className={e.severity === "critical" ? "text-red-400" : e.severity === "warn" ? "text-amber-300" : "text-slate-300"}>
            {e.at?.slice(11, 19)} [{e.category}] {e.message}
          </div>
        ))}
      </div>
    </div>
  );
}

export function Ledger() {
  const [rows] = useData<TradeOut[]>("/api/trades?limit=200");
  return (
    <div className="card overflow-x-auto">
      <h2 className="mb-2 text-sm font-semibold">Trade ledger (after charges)</h2>
      <table className="tbl">
        <thead><tr><th>exit</th><th>strategy</th><th>book</th><th>contract</th><th>qty</th><th>entry→exit</th><th>gross</th><th>charges</th><th>net</th><th>rules</th><th></th></tr></thead>
        <tbody>
          {(rows ?? []).map((t) => (
            <tr key={t.id}>
              <td>{t.exit_at?.slice(5, 16).replace("T", " ")}</td>
              <td>{t.strategy ?? "-"}</td>
              <td>{t.book}</td>
              <td>{t.instrument.split("|").slice(1).join(" ")}</td>
              <td>{t.qty}</td>
              <td>{t.entry} → {t.exit}</td>
              <td>{inr(t.gross)}</td>
              <td title={Object.entries(t.charges).map(([k, v]) => `${k} ${v}`).join("\n")}>{t.charges_total}</td>
              <td className={parseFloat(t.net) >= 0 ? "text-emerald-400" : "text-red-400"}>{inr(t.net)}</td>
              <td title={t.overrides.map((o) => JSON.stringify(o)).join("\n")}>{t.compliance ?? "-"}{t.overrides.length > 0 && ` (${t.overrides.length} override)`}</td>
              <td>{t.self_reported && <span className="pill bg-amber-800">SELF_REPORTED</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Books() {
  const [d] = useData<{ books: BookStat[]; ai_what_if: Record<string, { trades: number; net_pnl: string; win_rate: number | null }> }>("/api/books", 60_000);
  return (
    <div className="card overflow-x-auto">
      <h2 className="mb-2 text-sm font-semibold">Books vs the PDH/PDL baseline (after charges)</h2>
      <table className="tbl">
        <thead><tr><th>book</th><th>trades</th><th>win</th><th>expectancy</th><th>PF</th><th>max DD</th><th>net</th><th>charges</th></tr></thead>
        <tbody>
          {(d?.books ?? []).map((b) => (
            <tr key={b.book} className={b.book.includes("pdh_pdl") ? "text-sky-300" : ""}>
              <td>{b.book}</td><td>{b.trades}</td><td>{Math.round(b.win_rate * 100)}%</td>
              <td>{inr(b.expectancy)}</td><td>{b.profit_factor ?? "-"}</td><td>{b.max_drawdown}</td>
              <td>{inr(b.net_pnl)}</td><td>{b.charges}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3 className="mt-3 text-xs font-semibold text-slate-400">AI what-if (shadow-book trades by verdict)</h3>
      <div className="flex flex-wrap gap-3 text-xs">
        {Object.entries(d?.ai_what_if ?? {}).map(([k, v]) => (
          <span key={k}>{k}: {v.trades} trades, net {inr(v.net_pnl)}, win {v.win_rate === null ? "-" : `${Math.round(v.win_rate * 100)}%`}</span>
        ))}
      </div>
    </div>
  );
}

export function Journal() {
  const [rows] = useData<JournalOut[]>("/api/journal", 120_000);
  const j = rows?.[0];
  return (
    <div className="card text-xs">
      <h2 className="mb-2 text-sm font-semibold">End-of-day review {j && `(${j.date})`}</h2>
      {!j && <p className="text-slate-500">No journal yet.</p>}
      {j?.ai_review && <p className="whitespace-pre-wrap">{j.ai_review}<span className="block text-slate-500">{j.ai_provider} · advisory</span></p>}
      {j && !j.ai_review && <p className="text-slate-500">No AI review (AI off or no key).</p>}
      {j && <pre className="mt-2 max-h-48 overflow-auto text-slate-400">{JSON.stringify(j.stats, null, 1)}</pre>}
    </div>
  );
}

export function Settings() {
  const [d] = useData<Record<string, unknown>>("/api/settings", 300_000);
  if (!d) return null;
  const keys = d.keys as Record<string, string | null>;
  return (
    <div className="card space-y-2 text-xs">
      <h2 className="text-sm font-semibold">Settings and keys (read-only here)</h2>
      {(d.notes as string[]).map((n) => <p key={n} className="text-amber-300">{n}</p>)}
      <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
        {Object.entries(keys).map(([k, v]) => (
          <div key={k} className="flex justify-between border-b border-slate-800"><span>{k}</span><span className={v ? "text-emerald-400" : "text-slate-500"}>{v ?? "not set"}</span></div>
        ))}
      </div>
      {["slots", "routing", "risk", "time_guards", "contracts", "gate"].map((k) => (
        <details key={k}><summary className="cursor-pointer text-slate-300">{k}</summary>
          <pre className="overflow-auto text-slate-400">{JSON.stringify(d[k], null, 1)}</pre></details>
      ))}
    </div>
  );
}
