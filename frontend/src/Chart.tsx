import { useEffect, useRef, useState } from "react";
import { createChart, ColorType, LineStyle, type IChartApi } from "lightweight-charts";
import { get } from "./api";

interface Candles {
  bars: { time: number; open: number; high: number; low: number; close: number }[];
  ema200: { time: number; value: number }[];
  pdh: number | null;
  pdl: number | null;
}

export function Chart({ underlyings }: { underlyings: string[] }) {
  const box = useRef<HTMLDivElement>(null);
  const [u, setU] = useState(underlyings[0] ?? "NIFTY");
  const [tf, setTf] = useState(1);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!box.current) return;
    const chart: IChartApi = createChart(box.current, {
      height: 300,
      layout: { background: { type: ColorType.Solid, color: "#0f172a" }, textColor: "#cbd5e1" },
      grid: { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        // axis labels in IST, like everything else on the page (data is stored in UTC)
        tickMarkFormatter: (t: number) => {
          const d = new Date(t * 1000);
          const hm = d.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false });
          return hm === "09:15" ? d.toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short" }) : hm;
        },
      },
      localization: {
        timeFormatter: (t: number) =>
          new Date(t * 1000).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" }),
      },
    });
    const candles = chart.addCandlestickSeries();
    const ema = chart.addLineSeries({ color: "#38bdf8", lineWidth: 1 });
    let alive = true;
    const load = () =>
      get<Candles>(`/api/candles?u=${u}&tf=${tf}`)
        .then((d) => {
          if (!alive) return;
          setErr(null);
          candles.setData(d.bars as never);
          ema.setData(d.ema200 as never);
          for (const [p, c] of [[d.pdh, "#22c55e"], [d.pdl, "#ef4444"]] as const)
            if (p !== null) candles.createPriceLine({ price: p, color: c, lineStyle: LineStyle.Dashed, lineWidth: 1, title: c === "#22c55e" ? "PDH" : "PDL", axisLabelVisible: true });
        })
        .catch((e: Error) => alive && setErr(e.message));
    load();
    const t = setInterval(load, 30_000);
    const ro = new ResizeObserver(() => chart.applyOptions({ width: box.current?.clientWidth ?? 300 }));
    ro.observe(box.current);
    return () => {
      alive = false;
      clearInterval(t);
      ro.disconnect();
      chart.remove();
    };
  }, [u, tf]);

  return (
    <div className="card">
      <div className="mb-2 flex gap-2 text-xs">
        {underlyings.map((x) => (
          <button key={x} className={`pill ${x === u ? "bg-sky-700" : "bg-slate-800"}`} onClick={() => setU(x)}>{x}</button>
        ))}
        {[1, 15].map((x) => (
          <button key={x} className={`pill ${x === tf ? "bg-sky-700" : "bg-slate-800"}`} onClick={() => setTf(x)}>{x}m</button>
        ))}
        <span className="text-slate-500">200 EMA · PDH/PDL</span>
        {err && <span className="text-red-400">{err}</span>}
      </div>
      <div ref={box} className="w-full" />
    </div>
  );
}
