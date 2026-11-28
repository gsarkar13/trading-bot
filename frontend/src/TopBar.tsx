import { useState } from "react";
import { post, inr } from "./api";
import type { Mode, Status } from "./types";

const MODES: Mode[] = ["PAPER", "SEMI", "LIVE"];

export function TopBar({ s, onError }: { s: Status; onError: (m: string) => void }) {
  const [code, setCode] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const act = (p: Promise<unknown>) => p.catch((e: Error) => onError(e.message));
  const halted = s.engine.halted;
  const tickAge = Object.entries(s.last_tick);

  return (
    <header className="card flex flex-wrap items-center gap-2 text-sm">
      <button
        className={`pill ${halted ? "bg-amber-700" : "bg-emerald-700"}`}
        title={halted ?? "taking new entries"}
        onClick={() => act(post(halted ? "/api/engine/resume" : "/api/engine/pause"))}
      >
        {halted ? "PAUSED" : "RUNNING"}
      </button>
      <div className="flex items-center gap-1">
        {MODES.map((m) => (
          <button
            key={m}
            className={`pill ${s.ceiling === m ? "bg-sky-700" : "bg-slate-800"}`}
            disabled={MODES.indexOf(m) > MODES.indexOf(s.config_ceiling)}
            title={MODES.indexOf(m) > MODES.indexOf(s.config_ceiling) ? `config ceiling is ${s.config_ceiling}` : ""}
            onClick={() => act(post("/api/ceiling", { mode: m }))}
          >
            {m}
          </button>
        ))}
      </div>
      {s.armed ? (
        <button className="pill bg-red-700" onClick={() => act(post("/api/arm/disarm"))}>ARMED · disarm</button>
      ) : code ? (
        <span className="flex items-center gap-1">
          <span className="text-xs text-slate-400">code {code}:</span>
          <input className="inp w-16" value={typed} onChange={(e) => setTyped(e.target.value)} />
          <button
            className="btn"
            onClick={() => act(post("/api/arm/confirm", { code: typed }).then(() => setCode(null)))}
          >
            confirm
          </button>
        </span>
      ) : (
        <button
          className="pill bg-slate-800"
          onClick={() => act(post<{ code: string }>("/api/arm/request").then((r) => setCode(r.code)))}
        >
          arm real money
        </button>
      )}
      <span
        className={`pill ${s.host.live_possible ? "bg-emerald-900" : "bg-slate-800"}`}
        title={s.host.live_blockers.join("; ") || "LIVE possible here"}
      >
        {s.host.label} · {s.host.role} · IP {s.host.static_ip_ok ? "ok" : "not ok"} · LIVE here:{" "}
        {s.host.live_possible ? "yes" : "no"}
      </span>
      {tickAge.map(([u, t]) => (
        <span key={u} className="pill bg-slate-800" title={`last tick ${t ?? "never"}`}>
          {u} {s.spot[u]?.toFixed(2) ?? "-"}
        </span>
      ))}
      <span className="pill bg-slate-800">VIX {s.vix?.toFixed(2) ?? "-"}</span>
      <span className="pill bg-slate-800" title="clock drift vs NTP">
        drift {s.host.clock_drift_ms === null ? "?" : `${Math.round(s.host.clock_drift_ms)}ms`}
      </span>
      {Object.entries(s.order_rate).map(([n, r]) => (
        <span key={n} className="pill bg-slate-800" title={`${n} order rate`}>
          {n.replace("paper:", "")} {r.last_second}/{r.per_second_limit}s
        </span>
      ))}
      <span className="ml-auto flex flex-wrap gap-2 text-xs">
        <span>paper {inr(s.pnl.paper.net)} ({s.pnl.paper.trades})</span>
        <span>real {inr(s.pnl.real.net)} ({s.pnl.real.trades})</span>
        <span className={s.breakers.PAPER.tripped || s.breakers.REAL.tripped ? "text-red-400" : "text-emerald-400"}>
          breakers {s.breakers.PAPER.tripped || s.breakers.REAL.tripped ? "TRIPPED" : "ok"}
        </span>
      </span>
    </header>
  );
}
