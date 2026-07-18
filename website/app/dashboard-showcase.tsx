"use client";

import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

type View = "overview" | "records" | "retrieval";

const views: { id: View; label: string; number: string; detail: string }[] = [
  { id: "overview", label: "Overview", number: "01", detail: "Health, volume, and quality" },
  { id: "records", label: "Records", number: "02", detail: "Source-linked canonical memory" },
  { id: "retrieval", label: "Retrieval lab", number: "03", detail: "Ranked evidence and trace" },
];

const recordRows = [
  { title: "Prefers concise release summaries", type: "Preference", scope: "user_aria", confidence: "98%" },
  { title: "Durability suite passed", type: "Event", scope: "agent_release", confidence: "96%" },
  { title: "Customer migration window", type: "Commitment", scope: "org_northstar", confidence: "93%" },
  { title: "Escalate billing anomalies", type: "Instruction", scope: "agent_support", confidence: "89%" },
];

const retrievalRows = [
  ["0.94", "Durability suite passed", "event / deploy_118"],
  ["0.87", "Release evidence retained", "conversation / thread_420"],
  ["0.81", "Production rollout approved", "document / plan_77"],
];

export function DashboardShowcase() {
  const [active, setActive] = useState<View>("overview");
  const [cycle, setCycle] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const tabsRef = useRef<HTMLDivElement>(null);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let timer = 0;
    const observer = new IntersectionObserver(([entry]) => {
      window.clearInterval(timer);
      if (entry.isIntersecting) {
        timer = window.setInterval(() => {
          setActive((current) => views[(views.findIndex((view) => view.id === current) + 1) % views.length].id);
          setCycle((value) => value + 1);
        }, 5200);
      }
    }, { threshold: .25 });
    if (rootRef.current) observer.observe(rootRef.current);
    return () => { observer.disconnect(); window.clearInterval(timer); };
  }, [cycle]);

  useEffect(() => {
    const index = views.findIndex((view) => view.id === active);
    const tabs = tabsRef.current;
    const tab = tabRefs.current[index];
    if (!tabs || !tab || tabs.scrollWidth <= tabs.clientWidth) return;
    tabs.scrollTo({
      left: tab.offsetLeft - (tabs.clientWidth - tab.offsetWidth) / 2,
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
    });
  }, [active]);

  const choose = (view: View) => {
    setActive(view);
    setCycle((value) => value + 1);
  };

  const moveTabFocus = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let target = index;
    if (event.key === "ArrowRight") target = (index + 1) % views.length;
    else if (event.key === "ArrowLeft") target = (index - 1 + views.length) % views.length;
    else if (event.key === "Home") target = 0;
    else if (event.key === "End") target = views.length - 1;
    else return;
    event.preventDefault();
    choose(views[target].id);
    window.requestAnimationFrame(() => tabRefs.current[target]?.focus());
  };

  return <div className="landing-dashboard" ref={rootRef}>
    <div className="landing-dash-tabs" ref={tabsRef} role="tablist" aria-label="Dashboard showcase views">
      {views.map((view, index) => <button
        key={view.id}
        role="tab"
        aria-selected={active === view.id}
        tabIndex={active === view.id ? 0 : -1}
        ref={(node) => { tabRefs.current[index] = node; }}
        onClick={() => choose(view.id)}
        onKeyDown={(event) => moveTabFocus(event, index)}
      ><span>{view.number}</span><div><b>{view.label}</b><small>{view.detail}</small></div><i /></button>)}
    </div>

    <div className="landing-dash-frame">
      <aside aria-hidden="true">
        <div className="landing-dash-mark"><span>N</span><b>NarratorDB</b></div>
        <div className="landing-dash-project"><span>PROJECT</span><b>Atlas / Production</b></div>
        <nav>{["Overview", "Canonical records", "Entities & scopes", "Retrieval lab", "Activity", "Integrations"].map((label, index) => <span className={(active === "overview" && index === 0) || (active === "records" && index === 1) || (active === "retrieval" && index === 3) ? "active" : ""} key={label}><i>{String(index + 1).padStart(2, "0")}</i>{label}</span>)}</nav>
        <div className="landing-dash-health"><i /> All systems healthy</div>
      </aside>

      <section className="landing-dash-main">
        <header><div><span>ATLAS</span><b>/</b><span>PRODUCTION</span></div><div><span>PREVIEW DATA</span><i />WC</div></header>
        <div className="landing-dash-view" key={`${active}-${cycle}`}>
          {active === "overview" && <OverviewView />}
          {active === "records" && <RecordsView />}
          {active === "retrieval" && <RetrievalView />}
        </div>
      </section>
    </div>
    <div className="landing-dash-caption"><span><i /> LIVE PRODUCT PREVIEW</span><p>Project isolation · source provenance · inspectable retrieval · operational health</p><b>{views.findIndex((view) => view.id === active) + 1} / {views.length}</b></div>
  </div>;
}

function ViewHeading({ eyebrow, title, body, action }: { eyebrow: string; title: string; body: string; action: string }) {
  return <div className="landing-view-head"><div><span>{eyebrow}</span><h3>{title}</h3><p>{body}</p></div><button>{action} ↗</button></div>;
}

function OverviewView() {
  const metrics = [["Canonical records", "18,429", "+1,284 this month"], ["Retrievals / 24h", "42,817", "+8.4% today"], ["p50 latency", "19.05 ms", "p95 · 48.2 ms"], ["Evidence attached", "97.4%", "target ≥ 95%"]];
  return <>
    <ViewHeading eyebrow="01 / OVERVIEW" title="Production memory at a glance." body="One operating view across records, retrieval, and project health." action="Export report" />
    <div className="landing-metrics">{metrics.map(([label, value, detail], index) => <article key={label} className={index === 0 ? "orange" : ""}><span>{label}</span><b>{value}</b><small>{detail}</small><div>{[42, 55, 48, 68, 61, 79, 72, 90].map((height, bar) => <i style={{ height: `${Math.max(18, height - index * 5)}%`, animationDelay: `${bar * 45}ms` }} key={bar} />)}</div></article>)}</div>
    <div className="landing-overview-lower"><article><div className="landing-panel-head"><span>MEMORY TRAFFIC</span><b>61,246 operations</b></div><div className="landing-big-chart">{[42,51,47,63,58,71,66,82,74,89,80,96,86,77].map((height, index) => <i style={{ height: `${height}%`, animationDelay: `${index * 35}ms` }} key={index} />)}</div><footer><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>NOW</span></footer></article><article className="landing-health-card"><div className="landing-panel-head"><span>PRODUCTION HEALTH</span><em>HEALTHY</em></div>{[["Ingestion workers", "12 / 12"], ["Retrieval index", "Current"], ["Queue depth", "24"], ["Last commit", "18 sec"]].map(([label, value]) => <p key={label}><span>{label}</span><b>{value}</b><i /></p>)}</article></div>
  </>;
}

function RecordsView() {
  return <>
    <ViewHeading eyebrow="02 / MEMORY" title="Every record keeps its source." body="Inspect what was retained, why it exists, and which scope owns it." action="Filter records" />
    <div className="landing-record-tools"><div>Search records, sources, or scopes</div><span>ALL TYPES</span><span>VERIFIED</span></div>
    <div className="landing-record-table"><div className="landing-record-header"><span>RECORD</span><span>TYPE</span><span>SCOPE</span><span>CONFIDENCE</span><i /></div>{recordRows.map((record, index) => <article style={{ animationDelay: `${index * 85}ms` }} key={record.title}>
      <div className="landing-record-identity"><b>{record.title}</b><small>mem_{8 - index}Q{index + 2}F · source attached</small></div>
      <div className="landing-record-field"><small>TYPE</small><span>{record.type}</span></div>
      <div className="landing-record-field"><small>SCOPE</small><code>{record.scope}</code></div>
      <div className="landing-record-field"><small>CONFIDENCE</small><strong>{record.confidence}</strong></div>
      <button aria-label={`Inspect ${record.title}`}>↗</button>
    </article>)}</div>
    <div className="landing-record-foot"><span><i /> Original sources retained</span><b>4 of 18,429 records</b></div>
  </>;
}

function RetrievalView() {
  return <>
    <ViewHeading eyebrow="03 / RETRIEVAL" title="Test retrieval before shipping it." body="See ranked evidence, source links, and latency behind the final context." action="Copy response" />
    <div className="landing-retrieval-grid"><section><span>QUERY</span><p>What passed the durability suite, and what evidence supports it?</p><div><small>SCOPE</small><code>project_atlas</code></div><div><small>RANKING</small><code>semantic + lexical + temporal</code></div><button>Run retrieval <b>↗</b></button></section><section><div className="landing-result-head"><span>RANKED EVIDENCE</span><b>3 results · 19.05 ms</b></div>{retrievalRows.map(([score, title, source], index) => <article style={{ animationDelay: `${index * 100}ms` }} key={title}><strong>0{index + 1}</strong><b>{score}</b><div><h4>{title}</h4><p>The release candidate passed the complete durability suite in the production-like environment.</p><small>{source} · source retained</small></div></article>)}<footer><span>CONTEXT READY</span><p>Evidence returned with the original deployment event attached.</p></footer></section></div>
  </>;
}
