import { useEffect, useState } from "react";
import { ApiError, get, post } from "./api";
import { Chart } from "./Chart";
import { Checklists, Positions, SemiTasks } from "./Panels";
import { Books, Events, Journal, Ledger, Settings, Strategies } from "./Tables";
import { TopBar } from "./TopBar";
import type { SignalOut, Status } from "./types";

function Login({ done }: { done: () => void }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState<string | null>(null);
  return (
    <form className="card mx-auto mt-24 max-w-sm space-y-3" onSubmit={(e) => {
      e.preventDefault();
      post("/api/login", { password: pw }).then(done).catch((x: Error) => setErr(x.message));
    }}>
      <h1 className="text-lg font-semibold">Algo Desk</h1>
      <input className="inp w-full" type="password" autoFocus placeholder="dashboard password" value={pw} onChange={(e) => setPw(e.target.value)} />
      <button className="btn w-full" type="submit">Log in</button>
      {err && <p className="text-sm text-red-400">{err}</p>}
    </form>
  );
}

const TABS = ["Trade", "Strategies", "Ledger", "Events", "Review", "Settings"] as const;

export default function App() {
  const [s, setS] = useState<Status | null>(null);
  const [need, setNeed] = useState(false);
  const [signals, setSignals] = useState<SignalOut[]>([]);
  const [tab, setTab] = useState<(typeof TABS)[number]>("Trade");
  const [err, setErr] = useState<string | null>(null);

  const start = () => {
    setNeed(false);
    get<Status>("/api/status").then(setS).catch((e: ApiError) => e.status === 401 && setNeed(true));
  };
  useEffect(start, []);
  useEffect(() => {
    if (need || !s) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    let ws: WebSocket | null = null;
    let stop = false;
    const connect = () => {
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onmessage = (m) => setS(JSON.parse(m.data));
      ws.onclose = (e) => {
        if (e.code === 4401) setNeed(true);
        else if (!stop) setTimeout(connect, 2000);
      };
    };
    connect();
    const t = setInterval(() => get<SignalOut[]>("/api/signals?limit=50").then(setSignals).catch(() => undefined), 10_000);
    get<SignalOut[]>("/api/signals?limit=50").then(setSignals).catch(() => undefined);
    return () => { stop = true; ws?.close(); clearInterval(t); };
  }, [need, s === null]);
  useEffect(() => {
    if (!err) return;
    const t = setTimeout(() => setErr(null), 6000);
    return () => clearTimeout(t);
  }, [err]);

  if (need) return <Login done={start} />;
  if (!s) return <p className="p-6 text-slate-400">Connecting…</p>;
  const crit = s.alerts.filter((a) => a.level === "critical").slice(-3);
  return (
    <div className="mx-auto max-w-7xl space-y-3 p-2 sm:p-4">
      <TopBar s={s} onError={setErr} />
      {err && <div className="card border-red-700 text-sm text-red-300">{err}</div>}
      {s.engine.halted && <div className="card border-amber-700 text-sm text-amber-300">New entries stopped: {s.engine.halted}</div>}
      {crit.map((a, i) => <div key={i} className="card border-red-800 text-xs text-red-300">{a.at.slice(11, 19)} {a.message}</div>)}
      <nav className="flex gap-1 overflow-x-auto">
        {TABS.map((t) => <button key={t} className={`pill ${t === tab ? "bg-sky-700" : "bg-slate-800"}`} onClick={() => setTab(t)}>{t}</button>)}
      </nav>
      {tab === "Trade" && (
        <div className="grid gap-3 lg:grid-cols-3">
          <div className="space-y-3 lg:col-span-2">
            <Chart underlyings={Object.keys(s.last_tick).length ? Object.keys(s.last_tick) : ["NIFTY", "BANKNIFTY"]} />
            <Checklists signals={signals} s={s} />
          </div>
          <div className="order-first space-y-3 lg:order-none">
            <SemiTasks s={s} onError={setErr} />
            <Positions s={s} onError={setErr} />
          </div>
        </div>
      )}
      {tab === "Strategies" && <><Strategies onError={setErr} /><Books /></>}
      {tab === "Ledger" && <Ledger />}
      {tab === "Events" && <Events />}
      {tab === "Review" && <><Journal /><Books /></>}
      {tab === "Settings" && <Settings />}
    </div>
  );
}
