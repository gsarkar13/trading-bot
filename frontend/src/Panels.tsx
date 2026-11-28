import { useState } from "react";
import { ago, post } from "./api";
import type { SignalOut, Status } from "./types";

export function Checklists({ signals, s }: { signals: SignalOut[]; s: Status }) {
  const latest = new Map<string, SignalOut>();
  for (const x of signals) if (!latest.has(`${x.strategy}|${x.underlying}`)) latest.set(`${x.strategy}|${x.underlying}`, x);
  return (
    <div className="card space-y-3">
      <h2 className="text-sm font-semibold">Checklists (latest signal per strategy)</h2>
      {Object.entries(s.last_tick).map(([u, t]) => (
        <div key={u} className="text-xs text-slate-400">
          {u} data: last tick {ago(t, s.now_ist)}
        </div>
      ))}
      {latest.size === 0 && <p className="text-xs text-slate-500">No signals yet today.</p>}
      {[...latest.values()].map((x) => (
        <div key={x.uid} className="rounded border border-slate-800 p-2 text-xs">
          <div className="mb-1 flex flex-wrap justify-between gap-1">
            <span className="font-medium">{x.strategy} · {x.underlying} · {x.direction}</span>
            <span className="text-slate-400">{ago(x.at, s.now_ist)}</span>
          </div>
          <div className="mb-1 text-slate-300">{x.outcome}</div>
          <dl className="grid grid-cols-2 gap-x-3 sm:grid-cols-3">
            {Object.entries(x.checklist).map(([k, v]) => (
              <div key={k} className="flex justify-between gap-2 border-b border-slate-800/60">
                <dt className="text-slate-500">{k}</dt>
                <dd className="truncate">{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
              </div>
            ))}
          </dl>
          <div className="mt-1">
            AI:{" "}
            {x.ai ? (
              <span className={x.ai.approve ? "text-emerald-400" : "text-amber-400"}>
                {x.ai.approve ? "approve" : "disapprove"} ({x.ai.confidence}) - {x.ai.reason}
              </span>
            ) : (
              <span className="text-slate-500">no verdict (advisory only)</span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

export function Positions({ s, onError }: { s: Status; onError: (m: string) => void }) {
  const [phrase, setPhrase] = useState("");
  const act = (p: Promise<unknown>) => p.catch((e: Error) => onError(e.message));
  return (
    <div className="card space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <input className="inp w-28" placeholder="type FLATTEN" value={phrase} onChange={(e) => setPhrase(e.target.value)} />
        <button
          className="btn-red flex-1"
          disabled={phrase.trim().toUpperCase() !== "FLATTEN"}
          onClick={() => act(post("/api/flatten", { phrase }).then(() => setPhrase("")))}
        >
          EMERGENCY FLATTEN ALL
        </button>
      </div>
      {s.positions.length === 0 && <p className="text-xs text-slate-500">No open positions.</p>}
      {s.positions.map((p) => (
        <div key={p.uid} className={`rounded border p-2 text-xs ${p.book.startsWith("SHADOW") ? "border-slate-800 opacity-70" : "border-sky-800"}`}>
          <div className="flex justify-between">
            <span className="font-medium">{p.strategy} · {p.book} · {p.mode}</span>
            <span>{p.state}</span>
          </div>
          <div>{p.key.replaceAll("|", " ")} · qty {p.qty}</div>
          <div>
            strategy stop (engine, on index) {p.stop.toFixed(2)} · target {p.target?.toFixed(2) ?? "trail"}
          </div>
          <div className="text-slate-400">Resting at the broker: the wider backstop SL-LMT on the premium.</div>
          {!p.book.startsWith("SHADOW") && (
            <div className="mt-1 flex flex-wrap gap-1">
              {[["cost", "SL to cost"], ["trail", "Trail SL"], ["+5", "+5 pts"], ["-5", "-5 pts"]].map(([a, label]) => (
                <button key={a} className="btn" onClick={() => act(post(`/api/positions/${p.uid}/stop`, { action: a }))}>
                  {label}
                </button>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

export function SemiTasks({ s, onError }: { s: Status; onError: (m: string) => void }) {
  const [price, setPrice] = useState<Record<string, string>>({});
  if (s.semi_tasks.length === 0) return null;
  const act = (p: Promise<unknown>) => p.catch((e: Error) => onError(e.message));
  return (
    <div className="card space-y-2">
      <h2 className="text-sm font-semibold text-amber-300">SEMI order tasks</h2>
      {s.semi_tasks.map((t) => (
        <div key={t.uid} className="rounded border border-amber-800 p-2 text-xs">
          <pre className="whitespace-pre-wrap">{t.message}</pre>
          <div className="mt-1 flex flex-wrap items-center gap-1">
            <input className="inp w-24" placeholder="price" value={price[t.uid] ?? ""} onChange={(e) => setPrice({ ...price, [t.uid]: e.target.value })} />
            {t.status === "SENT" && (
              <>
                <button className="btn" onClick={() => act(post(`/api/semi/${t.uid}/placed`, { price: price[t.uid] }))}>Placed at ₹</button>
                <button className="btn" onClick={() => act(post(`/api/semi/${t.uid}/skip`))}>Skipped</button>
              </>
            )}
            {t.status === "CONFIRMED" && (
              <button className="btn" onClick={() => act(post(`/api/semi/${t.uid}/stop-placed`))}>Stop placed</button>
            )}
            {(t.status === "CONFIRMED" || t.status === "STOP_SEEN") && (
              <button className="btn" onClick={() => act(post(`/api/semi/${t.uid}/exited`, { price: price[t.uid] }))}>Exited at ₹</button>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
