"use client";

import { useMemo, useState } from "react";
import { benchmarkResults, BenchmarkKey, CategoryScores, providerName } from "./research-data";

const benchmarkLabels: Record<BenchmarkKey, string> = {
  longmemeval: "LongMemEval",
  locomo: "LoCoMo",
  "beam-1m": "BEAM · 1M",
  "beam-10m": "BEAM · 10M",
};

const categoryLabels: [keyof CategoryScores, string][] = [
  ["user", "Single-session · user"],
  ["assistant", "Single-session · assistant"],
  ["preference", "Preference"],
  ["update", "Knowledge update"],
  ["temporal", "Temporal reasoning"],
  ["multi", "Multi-session"],
];

export function ResearchWorkbench({ compact = false }: { compact?: boolean }) {
  const [benchmark, setBenchmark] = useState<BenchmarkKey>("longmemeval");
  const rows = useMemo(
    () => benchmarkResults.filter((result) => result.benchmark === benchmark).sort((a, b) => b.score - a.score),
    [benchmark],
  );
  const [selectedId, setSelectedId] = useState("narratordb");
  const selected = rows.find((row) => row.providerId === selectedId) ?? rows[0];

  return (
    <div className={`research-workbench ${compact ? "compact" : ""}`}>
      <div className="benchmark-controls" role="tablist" aria-label="Benchmark dataset">
        {(Object.keys(benchmarkLabels) as BenchmarkKey[]).map((key) => (
          <button
            type="button"
            role="tab"
            aria-selected={benchmark === key}
            className={benchmark === key ? "active" : ""}
            onClick={() => { setBenchmark(key); setSelectedId("narratordb"); }}
            key={key}
          >
            {benchmarkLabels[key]}
          </button>
        ))}
      </div>

      <div className="ranking-plot" role="tabpanel" aria-label={`${benchmarkLabels[benchmark]} published benchmark results`}>
        <div className="plot-scale" aria-hidden="true"><span>0</span><span>25</span><span>50</span><span>75</span><span>100%</span></div>
        {rows.map((row) => (
          <button
            type="button"
            className={`ranking-row ${row.providerId === "narratordb" ? "narrator" : ""} ${selected?.providerId === row.providerId ? "selected" : ""}`}
            onClick={() => setSelectedId(row.providerId)}
            aria-pressed={selected?.providerId === row.providerId}
            aria-label={`${providerName(row.providerId)}: ${row.score}% ${row.metric}`}
            key={`${row.providerId}-${row.benchmark}`}
          >
            <span className="ranking-name">{providerName(row.providerId)}</span>
            <span className="ranking-track"><i style={{ "--score": `${row.score}%` } as React.CSSProperties} /></span>
            <b className="ranking-value">{row.score}%</b>
          </button>
        ))}
      </div>

      {selected && (
        <div className="selected-method" aria-live="polite">
          <div><span>{providerName(selected.providerId)}</span><b>{selected.metric}</b></div>
          <p>{selected.configuration}</p>
          <a href={selected.source.url} target="_blank" rel="noreferrer">{selected.source.label} ↗</a>
        </div>
      )}
      <p className="comparison-warning">Published context, not a controlled leaderboard. Models, cutoffs, prompts, token budgets, and infrastructure differ.</p>
    </div>
  );
}

export function CategoryComparison() {
  const options = benchmarkResults.filter((result) => result.benchmark === "longmemeval" && result.categories);
  const [providerId, setProviderId] = useState("narratordb");
  const selected = options.find((result) => result.providerId === providerId) ?? options[0];

  return (
    <div className="category-comparison">
      <div className="provider-controls" aria-label="Choose category profile">
        {options.map((result) => (
          <button type="button" aria-pressed={selected.providerId === result.providerId} className={selected.providerId === result.providerId ? "active" : ""} onClick={() => setProviderId(result.providerId)} key={result.providerId}>
            {providerName(result.providerId)}
          </button>
        ))}
      </div>
      <div className="category-plot" role="img" aria-label={`${providerName(selected.providerId)} LongMemEval accuracy by question category`}>
        {categoryLabels.map(([key, label]) => {
          const value = selected.categories?.[key] ?? 0;
          return <div className="category-row" key={key}><span>{label}</span><i><em style={{ "--score": `${value}%` } as React.CSSProperties} /></i><b>{value}%</b></div>;
        })}
      </div>
      <div className="category-config"><span>{providerName(selected.providerId)}</span><p>{selected.configuration}</p><a href={selected.source.url} target="_blank" rel="noreferrer">Source ↗</a></div>
    </div>
  );
}

export function EfficiencyPlot() {
  const points = benchmarkResults.filter((result) => result.benchmark === "longmemeval" && result.latencyP50);
  const [selectedId, setSelectedId] = useState("narratordb");
  const selected = points.find((point) => point.providerId === selectedId) ?? points[0];
  const minLog = Math.log10(10);
  const maxLog = Math.log10(3000);
  const x = (value: number) => 4 + ((Math.log10(value) - minLog) / (maxLog - minLog)) * 92;
  const y = (score: number) => 100 - ((score - 75) / 25) * 100;

  return (
    <div className="efficiency-wrap">
      <div className="efficiency-plot" role="img" aria-label="Published LongMemEval score plotted against median retrieval latency on a logarithmic scale">
        <div className="y-label">Score</div>
        <div className="efficiency-grid" aria-hidden="true"><i /><i /><i /><i /></div>
        {points.map((point) => (
          <button
            type="button"
            className={`efficiency-point ${point.providerId === "narratordb" ? "narrator" : ""} ${selected.providerId === point.providerId ? "selected" : ""}`}
            style={{ "--x": `${x(point.latencyP50 ?? 10)}%`, "--y": `${y(point.score)}%` } as React.CSSProperties}
            onClick={() => setSelectedId(point.providerId)}
            aria-pressed={selected.providerId === point.providerId}
            aria-label={`${providerName(point.providerId)}, ${point.score}% and ${point.latencyP50} milliseconds median`}
            key={point.providerId}
          ><i /><span>{providerName(point.providerId)}</span></button>
        ))}
        <div className="x-ticks" aria-hidden="true"><span>10 ms</span><span>100 ms</span><span>1,000 ms</span><span>3,000 ms</span></div>
      </div>
      <div className="efficiency-detail" aria-live="polite">
        <b>{providerName(selected.providerId)}</b>
        <span>{selected.score}% · {selected.latencyP50} ms p50 · {selected.infrastructure}</span>
        <p>Latency definitions align imperfectly and cross local and managed infrastructure. Use this as operational context, not controlled performance proof.</p>
      </div>
    </div>
  );
}
