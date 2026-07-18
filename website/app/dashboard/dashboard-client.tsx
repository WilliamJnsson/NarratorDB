"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { dashboardViews, type DashboardView } from "./dashboard-config";

type RecordStatus = "verified" | "processing" | "archived";
type MemoryRecord = {
  id: string; title: string; source: string; scope: string; type: string; status: RecordStatus;
  confidence: number; updated: string; created: string; tags: string[]; excerpt: string;
};
type ActivityItem = { id: string; method: string; endpoint: string; status: number; latency: number; key: string; time: string; kind: string };
type ApiKey = { id: string; name: string; token: string; scope: string; env: string; lastUsed: string; active: boolean };
type Member = { id: string; name: string; email: string; initials: string; role: string; scope: string; status: string };
type Integration = { id: string; name: string; kind: string; detail: string; status: "ready" | "planned" | "connected" };
type PreviewState = { records: MemoryRecord[]; keys: ApiKey[]; members: Member[]; integrations: Integration[] };

const recordsSeed: MemoryRecord[] = [
  { id: "mem_8Q2F", title: "Prefers concise release summaries", source: "conversation / thread_420", scope: "user_aria", type: "Preference", status: "verified", confidence: .98, updated: "18 sec ago", created: "Jul 15, 16:42", tags: ["communication", "product"], excerpt: "Aria prefers release summaries that lead with impact and stay under six bullets." },
  { id: "mem_7W9K", title: "Durability suite passed", source: "event / deploy_118", scope: "agent_release", type: "Event", status: "verified", confidence: .96, updated: "2 min ago", created: "Jul 15, 16:39", tags: ["release", "qa"], excerpt: "The release candidate passed the complete durability suite in the production-like environment." },
  { id: "mem_6M1P", title: "Customer migration window", source: "document / plan_77", scope: "org_northstar", type: "Commitment", status: "verified", confidence: .93, updated: "7 min ago", created: "Jul 15, 16:34", tags: ["migration", "customer"], excerpt: "Northstar scheduled the final migration window for Friday at 09:00 UTC." },
  { id: "mem_5A8R", title: "Escalate billing anomalies", source: "policy / support_09", scope: "agent_support", type: "Instruction", status: "processing", confidence: .89, updated: "11 min ago", created: "Jul 15, 16:30", tags: ["support", "billing"], excerpt: "Billing anomalies above the defined threshold must be routed to the finance escalation queue." },
  { id: "mem_4L3T", title: "Uses Python for evaluation jobs", source: "conversation / thread_391", scope: "user_mika", type: "Profile", status: "verified", confidence: .91, updated: "24 min ago", created: "Jul 15, 16:17", tags: ["developer", "python"], excerpt: "Mika runs evaluation and reporting jobs in Python and deploys the service layer in TypeScript." },
  { id: "mem_3C6N", title: "Previous retrieval configuration", source: "config / retrieval_v2", scope: "project_atlas", type: "Configuration", status: "archived", confidence: .87, updated: "1 hr ago", created: "Jul 15, 15:41", tags: ["retrieval", "history"], excerpt: "Historical hybrid retrieval weights retained for audit and rollback comparisons." },
];

const activitySeed: ActivityItem[] = [
  { id: "req_91J7", method: "POST", endpoint: "/v1/memory/search", status: 200, latency: 19, key: "production-agent", time: "16:44:09", kind: "retrieve" },
  { id: "req_84Y2", method: "POST", endpoint: "/v1/memory/ingest", status: 201, latency: 42, key: "support-worker", time: "16:43:51", kind: "ingest" },
  { id: "req_73Q5", method: "PATCH", endpoint: "/v1/records/mem_8Q2F", status: 200, latency: 27, key: "production-agent", time: "16:43:17", kind: "update" },
  { id: "req_62P4", method: "POST", endpoint: "/v1/memory/search", status: 422, latency: 11, key: "eval-runner", time: "16:42:58", kind: "error" },
  { id: "req_51D8", method: "POST", endpoint: "/v1/memory/ingest", status: 201, latency: 48, key: "production-agent", time: "16:42:22", kind: "ingest" },
  { id: "req_40B6", method: "DELETE", endpoint: "/v1/records/mem_2H9S", status: 204, latency: 24, key: "admin-console", time: "16:40:36", kind: "delete" },
];

const stateSeed: PreviewState = {
  records: recordsSeed,
  keys: [
    { id: "key_1", name: "production-agent", token: "ndb_live_••••••••7K2Q", scope: "Read + write", env: "Production", lastUsed: "18 sec ago", active: true },
    { id: "key_2", name: "eval-runner", token: "ndb_test_••••••••1J9A", scope: "Read only", env: "Development", lastUsed: "6 min ago", active: true },
    { id: "key_3", name: "legacy-import", token: "ndb_live_••••••••9M4R", scope: "Ingest only", env: "Production", lastUsed: "24 days ago", active: false },
  ],
  members: [
    { id: "m1", name: "William Chen", email: "william@narratordb.dev", initials: "WC", role: "Account owner", scope: "All projects", status: "Active" },
    { id: "m2", name: "Maya Brooks", email: "maya@narratordb.dev", initials: "MB", role: "Project admin", scope: "Atlas", status: "Active" },
    { id: "m3", name: "Jon Bell", email: "jon@narratordb.dev", initials: "JB", role: "Project viewer", scope: "Atlas / production", status: "Active" },
  ],
  integrations: [
    { id: "python", name: "Python SDK", kind: "SDK", detail: "Synchronous and lower-level engine APIs.", status: "connected" },
    { id: "mcp", name: "MCP gateway", kind: "Protocol", detail: "Memory tools for compatible agents and environments.", status: "connected" },
    { id: "http", name: "HTTP API", kind: "API", detail: "Typed endpoints for every ingestion and retrieval path.", status: "ready" },
    { id: "jsonl", name: "JSONL bridge", kind: "Import", detail: "Stream standard I/O records without custom plumbing.", status: "ready" },
    { id: "langgraph", name: "LangGraph", kind: "Framework", detail: "Framework adapter over the stable Python boundary.", status: "planned" },
    { id: "agents", name: "Agents SDK", kind: "Framework", detail: "Persistent memory hooks for runs and handoffs.", status: "planned" },
  ],
};

const entities = [
  { id: "user_aria", type: "User", records: 184, activity: "18 sec ago", summary: "Product lead. Prefers concise, evidence-first release communication." },
  { id: "agent_support", type: "Agent", records: 1320, activity: "1 min ago", summary: "Customer support agent with billing and escalation policy context." },
  { id: "org_northstar", type: "Organization", records: 847, activity: "7 min ago", summary: "Enterprise migration account with deployment commitments and history." },
  { id: "project_atlas", type: "Project", records: 4419, activity: "11 min ago", summary: "Primary managed memory project spanning production and evaluation." },
  { id: "user_mika", type: "User", records: 76, activity: "24 min ago", summary: "Developer working primarily with Python evaluation workflows." },
  { id: "agent_release", type: "Agent", records: 393, activity: "2 min ago", summary: "Release operator tracking test evidence, rollouts, and incidents." },
];

const usageBars = [38, 46, 41, 58, 53, 66, 62, 74, 69, 82, 78, 91, 86, 76];
const latencyBars = [42, 51, 38, 63, 44, 71, 55, 47, 68, 58, 46, 62, 49, 40];

function cloneSeed(): PreviewState {
  return JSON.parse(JSON.stringify(stateSeed)) as PreviewState;
}

function StatusDot({ tone = "healthy", children }: { tone?: "healthy" | "warning" | "neutral" | "error"; children: React.ReactNode }) {
  return <span className={`dash-status ${tone}`}><i />{children}</span>;
}

function Bars({ values, tone = "orange", label }: { values: number[]; tone?: "orange" | "green" | "black"; label: string }) {
  return <div className={`dash-bars ${tone}`} role="img" aria-label={label}>{values.map((value, index) => <i key={`${value}-${index}`} style={{ height: `${value}%`, animationDelay: `${index * 34}ms` }} />)}</div>;
}

function EmptyState({ title, body }: { title: string; body: string }) {
  return <div className="dash-empty"><span>00</span><h3>{title}</h3><p>{body}</p></div>;
}

export function DashboardApp({ initialView }: { initialView: DashboardView }) {
  const [state, setState] = useState<PreviewState>(cloneSeed);
  const [hydrated, setHydrated] = useState(false);
  const [project, setProject] = useState("Atlas");
  const [environment, setEnvironment] = useState("Production");
  const [mobileOpen, setMobileOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      try {
        const stored = sessionStorage.getItem("narratordb-dashboard-preview");
        if (stored) setState(JSON.parse(stored));
      } catch { /* preview falls back to seed data */ }
      setHydrated(true);
    });
    return () => window.cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    sessionStorage.setItem("narratordb-dashboard-preview", JSON.stringify(state));
  }, [state, hydrated]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 2600);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const reset = () => {
    const fresh = cloneSeed();
    setState(fresh);
    sessionStorage.removeItem("narratordb-dashboard-preview");
    setNotice("Preview data reset");
  };

  return <main className="dashboard-root">
    <aside className={`dashboard-sidebar ${mobileOpen ? "open" : ""}`}>
      <div className="dash-brand-row">
        <Link className="dash-wordmark" href="/"><span>N</span><b>NarratorDB</b></Link>
        <button className="dash-mobile-close" onClick={() => setMobileOpen(false)} aria-label="Close navigation">×</button>
      </div>
      <div className="dash-preview-mark"><i /> Functional preview</div>
      <nav className="dashboard-nav" aria-label="Dashboard navigation">
        {["Workspace", "Memory", "Operate", "Organization"].map((group) => <div className="dash-nav-group" key={group}>
          <span>{group}</span>
          {dashboardViews.filter((item) => item.group === group).map((item) => <Link href={item.id === "overview" ? "/dashboard" : `/dashboard/${item.id}`} aria-current={initialView === item.id ? "page" : undefined} onClick={() => setMobileOpen(false)} key={item.id}><i>{item.short}</i>{item.label}{item.id === "activity" && <em>6</em>}</Link>)}
        </div>)}
      </nav>
      <div className="dash-sidebar-foot"><StatusDot>All systems healthy</StatusDot><Link href="/">Return to site ↗</Link></div>
    </aside>

    {mobileOpen && <button className="dash-scrim" aria-label="Close navigation" onClick={() => setMobileOpen(false)} />}

    <section className="dashboard-canvas">
      <header className="dashboard-topbar">
        <button className="dash-menu-button" onClick={() => setMobileOpen(true)} aria-label="Open navigation">Menu</button>
        <div className="dash-context-selectors">
          <label><span>Project</span><select value={project} onChange={(event) => setProject(event.target.value)}><option>Atlas</option><option>Northstar</option><option>Sandbox</option></select></label>
          <label><span>Environment</span><select value={environment} onChange={(event) => setEnvironment(event.target.value)}><option>Production</option><option>Development</option></select></label>
        </div>
        <div className="dash-top-actions">
          <label className="dash-global-search"><span className="sr-only">Search dashboard</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search records or requests" /><kbd>⌘ K</kbd></label>
          <button className="dash-reset" onClick={reset}>Reset preview</button>
          <button className="dash-avatar" aria-label="Account menu">WC</button>
        </div>
      </header>
      <div className="dashboard-mobile-context"><b>{project}</b><span>/</span><b>{environment}</b><StatusDot>Healthy</StatusDot></div>

      <div className="dashboard-view" key={initialView}>
        {initialView === "overview" && <Overview records={state.records} />}
        {initialView === "records" && <Records records={state.records} setState={setState} globalQuery={query} notify={setNotice} />}
        {initialView === "entities" && <Entities globalQuery={query} notify={setNotice} />}
        {initialView === "retrieval" && <RetrievalLab records={state.records} environment={environment} />}
        {initialView === "activity" && <Activity globalQuery={query} />}
        {initialView === "integrations" && <Integrations state={state} setState={setState} notify={setNotice} />}
        {initialView === "keys" && <Keys state={state} setState={setState} environment={environment} notify={setNotice} />}
        {initialView === "team" && <Team state={state} setState={setState} notify={setNotice} />}
        {initialView === "usage" && <Usage />}
        {initialView === "settings" && <Settings project={project} environment={environment} notify={setNotice} />}
      </div>
    </section>
    {notice && <div className="dash-toast" role="status"><i>✓</i>{notice}</div>}
  </main>;
}

function PageHead({ index, title, body, actions }: { index: string; title: string; body: string; actions?: React.ReactNode }) {
  return <div className="dash-page-head"><div><span>{index}</span><h1>{title}</h1><p>{body}</p></div>{actions && <div className="dash-page-actions">{actions}</div>}</div>;
}

function Overview({ records }: { records: MemoryRecord[] }) {
  const verified = records.filter((record) => record.status === "verified").length;
  return <>
    <PageHead index="01 / OVERVIEW" title="Memory operations, without the black box." body="A live operating view of canonical records, retrieval quality, and project health." actions={<><button className="dash-button secondary">Export report</button><Link className="dash-button ready" href="/dashboard/retrieval">Run retrieval ↗</Link></>} />
    <div className="metric-grid">
      <article className="metric-card accent-orange"><span>Canonical records</span><b>18,429</b><small>+1,284 this month</small><Bars values={[34, 39, 45, 47, 54, 61, 58, 68, 74, 78]} label="Canonical records trend" /></article>
      <article className="metric-card"><span>Retrievals / 24h</span><b>42,817</b><small>+8.4% vs previous day</small><Bars values={[46, 51, 44, 63, 59, 71, 67, 76, 69, 82]} tone="black" label="Retrieval volume trend" /></article>
      <article className="metric-card"><span>p50 latency</span><b>19.05 <em>ms</em></b><small>p95 · 48.2 ms</small><Bars values={latencyBars.slice(0, 10)} tone="green" label="Retrieval latency trend" /></article>
      <article className="metric-card"><span>Evidence attached</span><b>97.4%</b><small>{verified} of {records.length} visible preview records verified</small><div className="metric-ring" style={{ "--value": "97.4%" } as React.CSSProperties}><i /></div></article>
    </div>

    <div className="dash-grid-main">
      <article className="dash-panel usage-panel"><div className="dash-panel-head"><div><span>Operations</span><h2>Memory traffic</h2></div><div className="dash-segment"><button className="active">24H</button><button>7D</button><button>30D</button></div></div><div className="traffic-summary"><div><b>61,246</b><span>Total operations</span></div><div><b>99.94%</b><span>Successful</span></div><div><b>0.06%</b><span>Errors</span></div></div><Bars values={usageBars} label="Operations over the last 24 hours" /><div className="chart-axis"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>Now</span></div></article>
      <article className="dash-panel health-panel"><div className="dash-panel-head"><div><span>Environment</span><h2>Production health</h2></div><StatusDot>Healthy</StatusDot></div><div className="health-list"><div><span>Ingestion workers</span><b>12 / 12</b><i className="healthy" /></div><div><span>Retrieval index</span><b>Current</b><i className="healthy" /></div><div><span>Queue depth</span><b>24</b><i className="warning" /></div><div><span>Last index commit</span><b>18 sec</b><i className="healthy" /></div></div><Link href="/dashboard/activity" className="panel-link">Open activity log <span>↗</span></Link></article>
    </div>

    <div className="dash-grid-lower">
      <article className="dash-panel"><div className="dash-panel-head"><div><span>Retrieval</span><h2>Quality signals</h2></div><span className="dash-faint">Last 24 hours</span></div><div className="signal-grid"><div><b>0.94</b><span>Mean top score</span></div><div><b>3.8</b><span>Evidence items / query</span></div><div><b>2.1%</b><span>Empty result rate</span></div></div><div className="quality-track"><i style={{ width: "94%" }} /><span>Target ≥ 0.90</span></div></article>
      <article className="dash-panel"><div className="dash-panel-head"><div><span>Sources</span><h2>Ingestion mix</h2></div><span className="dash-faint">18,429 records</span></div><div className="source-mix"><div style={{ "--size": "44%" } as React.CSSProperties}><i />Conversation <b>44%</b></div><div style={{ "--size": "28%" } as React.CSSProperties}><i />Documents <b>28%</b></div><div style={{ "--size": "18%" } as React.CSSProperties}><i />Events <b>18%</b></div><div style={{ "--size": "10%" } as React.CSSProperties}><i />Structured <b>10%</b></div></div></article>
    </div>
  </>;
}

function Records({ records, setState, globalQuery, notify }: { records: MemoryRecord[]; setState: React.Dispatch<React.SetStateAction<PreviewState>>; globalQuery: string; notify: (message: string) => void }) {
  const [localQuery, setLocalQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [selected, setSelected] = useState<string[]>([]);
  const [inspecting, setInspecting] = useState<MemoryRecord | null>(null);
  const search = `${globalQuery} ${localQuery}`.trim().toLowerCase();
  const filtered = records.filter((record) => (!search || JSON.stringify(record).toLowerCase().includes(search)) && (status === "all" || record.status === status));
  const updateRecords = (fn: (items: MemoryRecord[]) => MemoryRecord[]) => setState((current) => ({ ...current, records: fn(current.records) }));
  const bulkArchive = () => { updateRecords((items) => items.map((item) => selected.includes(item.id) ? { ...item, status: "archived" } : item)); setSelected([]); notify("Selected records archived"); };
  const bulkDelete = () => { updateRecords((items) => items.filter((item) => !selected.includes(item.id))); setSelected([]); setInspecting(null); notify("Selected preview records deleted"); };
  return <>
    <PageHead index="02 / MEMORY" title="Canonical records" body="Inspect the source, scope, history, and retrieval-ready interpretation behind every memory." actions={<button className="dash-button ready" onClick={() => notify("Ingestion endpoint copied")}>Copy ingest endpoint</button>} />
    <div className="dash-toolbar"><label className="dash-search"><span>Search</span><input value={localQuery} onChange={(event) => setLocalQuery(event.target.value)} placeholder="Record, source, scope, or tag" /></label><label><span>Status</span><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">All statuses</option><option value="verified">Verified</option><option value="processing">Processing</option><option value="archived">Archived</option></select></label><label><span>Type</span><select><option>All types</option><option>Preference</option><option>Event</option><option>Instruction</option></select></label><button className="dash-filter-button">More filters +</button></div>
    {selected.length > 0 && <div className="dash-bulk"><b>{selected.length} selected</b><button onClick={bulkArchive}>Archive</button><button className="danger" onClick={bulkDelete}>Delete</button><button onClick={() => setSelected([])}>Clear</button></div>}
    <div className="dash-table-wrap"><table className="dash-table records-table"><thead><tr><th><span className="sr-only">Select</span></th><th>Record</th><th>Type</th><th>Scope</th><th>Status</th><th>Confidence</th><th>Updated</th><th /></tr></thead><tbody>{filtered.map((record) => <tr key={record.id}><td><input type="checkbox" aria-label={`Select ${record.title}`} checked={selected.includes(record.id)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, record.id] : current.filter((id) => id !== record.id))} /></td><td><button className="record-title" onClick={() => setInspecting(record)}><b>{record.title}</b><span>{record.id} · {record.source}</span></button></td><td>{record.type}</td><td><code>{record.scope}</code></td><td><StatusDot tone={record.status === "verified" ? "healthy" : record.status === "processing" ? "warning" : "neutral"}>{record.status}</StatusDot></td><td>{Math.round(record.confidence * 100)}%</td><td>{record.updated}</td><td><button className="row-open" onClick={() => setInspecting(record)} aria-label={`Inspect ${record.title}`}>↗</button></td></tr>)}</tbody></table>{filtered.length === 0 && <EmptyState title="No records found" body="Change the active filters or reset the preview data." />}</div>
    <div className="table-footer"><span>Showing {filtered.length} of {records.length} preview records</span><div><button disabled>Previous</button><b>1</b><button disabled>Next</button></div></div>
    {inspecting && <RecordInspector record={inspecting} close={() => setInspecting(null)} updateRecords={updateRecords} notify={notify} />}
  </>;
}

function RecordInspector({ record, close, updateRecords, notify }: { record: MemoryRecord; close: () => void; updateRecords: (fn: (items: MemoryRecord[]) => MemoryRecord[]) => void; notify: (message: string) => void }) {
  const [tab, setTab] = useState("record");
  const verify = () => { updateRecords((items) => items.map((item) => item.id === record.id ? { ...item, status: "verified", confidence: Math.max(item.confidence, .96), updated: "just now" } : item)); notify("Record marked verified"); close(); };
  return <><button className="dash-drawer-scrim" onClick={close} aria-label="Close record inspector" /><aside className="dash-drawer" aria-label="Record inspector"><div className="drawer-head"><div><span>CANONICAL RECORD</span><b>{record.id}</b></div><button onClick={close} aria-label="Close">×</button></div><div className="drawer-title"><StatusDot tone={record.status === "verified" ? "healthy" : record.status === "processing" ? "warning" : "neutral"}>{record.status}</StatusDot><h2>{record.title}</h2><p>{record.excerpt}</p></div><div className="drawer-tabs"><button className={tab === "record" ? "active" : ""} onClick={() => setTab("record")}>Record</button><button className={tab === "history" ? "active" : ""} onClick={() => setTab("history")}>History</button><button className={tab === "json" ? "active" : ""} onClick={() => setTab("json")}>Raw JSON</button></div>{tab === "record" && <div className="drawer-body"><dl><div><dt>Source</dt><dd>{record.source}</dd></div><div><dt>Scope</dt><dd>{record.scope}</dd></div><div><dt>Type</dt><dd>{record.type}</dd></div><div><dt>Confidence</dt><dd>{Math.round(record.confidence * 100)}%</dd></div><div><dt>Created</dt><dd>{record.created}</dd></div></dl><div className="drawer-block"><span>PROVENANCE</span><p>Original source retained. Interpretation produced by the production extraction policy and linked to the immutable source event.</p></div><div className="tag-list">{record.tags.map((tag) => <span key={tag}>{tag}</span>)}</div></div>}{tab === "history" && <div className="drawer-body timeline"><div><i /><b>Record retrieved</b><span>18 seconds ago · score 0.94</span></div><div><i /><b>Interpretation verified</b><span>2 minutes ago · extraction-v3</span></div><div><i /><b>Source ingested</b><span>{record.created} · {record.source}</span></div></div>}{tab === "json" && <pre className="drawer-json">{JSON.stringify(record, null, 2)}</pre>}<div className="drawer-actions"><button className="dash-button ready" onClick={verify}>Mark verified</button><button className="dash-button secondary" onClick={() => notify("Record JSON copied")}>Copy JSON</button></div></aside></>;
}

function Entities({ globalQuery, notify }: { globalQuery: string; notify: (message: string) => void }) {
  const visible = entities.filter((entity) => !globalQuery || JSON.stringify(entity).toLowerCase().includes(globalQuery.toLowerCase()));
  return <><PageHead index="03 / SCOPES" title="Entities & scopes" body="Understand who and what owns memory, then inspect or remove the complete bounded history." actions={<button className="dash-button ready" onClick={() => notify("New scope simulation opened")}>Create scope +</button>} /><div className="entity-summary"><div><span>Active scopes</span><b>8,241</b></div><div><span>Users</span><b>6,903</b></div><div><span>Agents</span><b>182</b></div><div><span>Custom</span><b>1,156</b></div></div><div className="entity-grid">{visible.map((entity) => <article key={entity.id}><div className="entity-top"><span>{entity.type}</span><button aria-label={`Open ${entity.id}`}>↗</button></div><h2>{entity.id}</h2><p>{entity.summary}</p><div className="entity-meta"><span><b>{entity.records.toLocaleString()}</b> records</span><span>Active {entity.activity}</span></div><div className="entity-actions"><button onClick={() => notify(`${entity.id} opened`)}>Inspect scope</button><button className="danger" onClick={() => notify("Privacy deletion previewed; no live data changed")}>Privacy delete</button></div></article>)}</div>{visible.length === 0 && <EmptyState title="No scopes found" body="Search for a user, agent, organization, or project identifier." />}</>;
}

function RetrievalLab({ records, environment }: { records: MemoryRecord[]; environment: string }) {
  const [text, setText] = useState("What passed the durability suite, and what evidence supports it?");
  const [scope, setScope] = useState("project_atlas");
  const [topK, setTopK] = useState(4);
  const [running, setRunning] = useState(false);
  const [hasRun, setHasRun] = useState(true);
  const run = () => { setRunning(true); setHasRun(false); window.setTimeout(() => { setRunning(false); setHasRun(true); }, 760); };
  const resultRows = records.filter((record) => record.status !== "archived").slice(0, topK);
  return <><PageHead index="04 / RETRIEVAL" title="Retrieval lab" body="Test a query against production-like memory and inspect every ranking signal before shipping it." actions={<StatusDot>Preview index current</StatusDot>} /><div className="retrieval-layout"><section className="retrieval-controls"><div className="retrieval-control-head"><span>QUERY CONFIGURATION</span><b>{environment}</b></div><label><span>Query</span><textarea value={text} onChange={(event) => setText(event.target.value)} rows={5} /></label><div className="control-row"><label><span>Scope</span><select value={scope} onChange={(event) => setScope(event.target.value)}>{entities.map((entity) => <option key={entity.id}>{entity.id}</option>)}</select></label><label><span>Top K · {topK}</span><input type="range" min="1" max="6" value={topK} onChange={(event) => setTopK(Number(event.target.value))} /></label></div><div className="retrieval-options"><label><input type="checkbox" defaultChecked /> Semantic</label><label><input type="checkbox" defaultChecked /> Lexical</label><label><input type="checkbox" defaultChecked /> Temporal</label><label><input type="checkbox" defaultChecked /> Provenance</label></div><button className="dash-button ready wide" onClick={run} disabled={running || !text.trim()}>{running ? "Running retrieval…" : "Run retrieval"}<span>↗</span></button><div className="code-sample"><div><span>PYTHON</span><button>Copy</button></div><pre>{`result = ndb.search(\n  query=${JSON.stringify(text.slice(0, 28) + "…")},\n  scope=${JSON.stringify(scope)},\n  top_k=${topK}\n)`}</pre></div></section><section className={`retrieval-results ${running ? "loading" : ""}`}><div className="result-head"><div><span>RANKED EVIDENCE</span><b>{hasRun ? `${resultRows.length} results · 19.05 ms` : "Preparing index…"}</b></div><div className="result-tabs"><button className="active">Results</button><button>Response JSON</button><button>Trace</button></div></div>{running ? <div className="result-skeleton">{[1,2,3].map((item) => <i key={item} />)}</div> : <div className="result-list">{resultRows.map((record, index) => <article key={record.id} style={{ animationDelay: `${index * 70}ms` }}><div className="result-rank"><span>0{index + 1}</span><b>{(0.94 - index * .07).toFixed(2)}</b></div><div><h3>{record.title}</h3><p>{record.excerpt}</p><div className="result-signals"><span>semantic {(0.96 - index * .05).toFixed(2)}</span><span>lexical {(0.88 - index * .03).toFixed(2)}</span><span>temporal {(0.92 - index * .06).toFixed(2)}</span></div><small>{record.source} · {record.scope}</small></div><button aria-label={`Inspect ${record.title}`}>↗</button></article>)}</div>}<div className="retrieval-answer"><span>CONTEXT BLOCK</span><p>The release candidate passed the complete durability suite. Evidence is attached from <code>{scope}</code> with the original deployment event retained.</p><button>Copy context</button></div></section></div></>;
}

function Activity({ globalQuery }: { globalQuery: string }) {
  const [kind, setKind] = useState("all");
  const filtered = activitySeed.filter((item) => (!globalQuery || JSON.stringify(item).toLowerCase().includes(globalQuery.toLowerCase())) && (kind === "all" || item.kind === kind));
  return <><PageHead index="05 / OPERATE" title="Activity" body="Follow every ingest, retrieval, update, and error from API key to response." actions={<button className="dash-button secondary">Export logs</button>} /><div className="activity-overview"><div><span>Requests / 24h</span><b>61,246</b><Bars values={usageBars.slice(0, 9)} label="Request trend" /></div><div><span>Success rate</span><b>99.94%</b><Bars values={[88,92,89,96,93,97,96,98,99]} tone="green" label="Success rate trend" /></div><div><span>p95 latency</span><b>48.2 ms</b><Bars values={latencyBars.slice(0, 9)} tone="black" label="Latency trend" /></div></div><div className="activity-filter"><div className="dash-segment">{["all", "ingest", "retrieve", "update", "error"].map((item) => <button key={item} className={kind === item ? "active" : ""} onClick={() => setKind(item)}>{item}</button>)}</div><label><span>Range</span><select><option>Last 24 hours</option><option>Last 7 days</option><option>Last 30 days</option></select></label></div><div className="dash-table-wrap"><table className="dash-table"><thead><tr><th>Time</th><th>Request</th><th>Endpoint</th><th>Status</th><th>Latency</th><th>API key</th><th /></tr></thead><tbody>{filtered.map((item) => <tr key={item.id}><td>{item.time}</td><td><code>{item.method}</code><span className="table-sub">{item.id}</span></td><td><code>{item.endpoint}</code></td><td><StatusDot tone={item.status >= 400 ? "error" : "healthy"}>{item.status}</StatusDot></td><td>{item.latency} ms</td><td>{item.key}</td><td><button className="row-open">↗</button></td></tr>)}</tbody></table>{filtered.length === 0 && <EmptyState title="No requests found" body="Change the activity filter or global search." />}</div></>;
}

function Integrations({ state, setState, notify }: { state: PreviewState; setState: React.Dispatch<React.SetStateAction<PreviewState>>; notify: (message: string) => void }) {
  const connect = (id: string) => { setState((current) => ({ ...current, integrations: current.integrations.map((item) => item.id === id && item.status !== "planned" ? { ...item, status: "connected" } : item) })); notify("Integration connected in preview"); };
  return <><PageHead index="06 / CONNECT" title="Integrations" body="Bring NarratorDB into existing applications through stable SDK, protocol, and data boundaries." actions={<button className="dash-button secondary">View documentation ↗</button>} /><div className="integration-banner"><div><StatusDot>4 interfaces ready</StatusDot><h2>Your project can accept memory now.</h2><p>Use a scoped API key and the production endpoint to connect an application in minutes.</p></div><div><span>PROJECT ENDPOINT</span><code>https://api.narratordb.dev/v1/atlas</code><button onClick={() => notify("Project endpoint copied")}>Copy</button></div></div><div className="integration-grid">{state.integrations.map((item, index) => <article key={item.id}><div className="integration-icon">{String(index + 1).padStart(2, "0")}</div><div className="integration-copy"><span>{item.kind}</span><h2>{item.name}</h2><p>{item.detail}</p></div><div className="integration-foot"><StatusDot tone={item.status === "connected" ? "healthy" : item.status === "planned" ? "neutral" : "warning"}>{item.status}</StatusDot><button disabled={item.status === "planned"} onClick={() => connect(item.id)}>{item.status === "connected" ? "Configure" : item.status === "planned" ? "Join waitlist" : "Connect"} ↗</button></div></article>)}</div></>;
}

function Keys({ state, setState, environment, notify }: { state: PreviewState; setState: React.Dispatch<React.SetStateAction<PreviewState>>; environment: string; notify: (message: string) => void }) {
  const [revealed, setRevealed] = useState("");
  const create = () => { const key = { id: `key_${Date.now()}`, name: `preview-key-${state.keys.length + 1}`, token: "ndb_preview_A7p9K2m4X6q8", scope: "Read + write", env: environment, lastUsed: "Never", active: true }; setState((current) => ({ ...current, keys: [key, ...current.keys] })); setRevealed(key.token); notify("Preview key created"); };
  const revoke = (id: string) => { setState((current) => ({ ...current, keys: current.keys.map((key) => key.id === id ? { ...key, active: false } : key) })); notify("Preview key revoked"); };
  return <><PageHead index="07 / ACCESS" title="API keys" body="Issue environment-scoped credentials, inspect last use, and rotate access without losing the audit trail." actions={<button className="dash-button ready" onClick={create}>Create API key +</button>} />{revealed && <div className="key-reveal"><div><span>NEW KEY · COPY IT NOW</span><code>{revealed}</code><p>This preview value will be hidden when you leave the page.</p></div><button onClick={() => { navigator.clipboard?.writeText(revealed); notify("Preview key copied"); }}>Copy key</button><button className="close" onClick={() => setRevealed("")} aria-label="Hide key">×</button></div>}<div className="key-policy"><div><span>KEY POLICY</span><b>Project scoped · explicit environment · auditable</b></div><p>Keys are masked after creation. Production keys should be rotated every 90 days and never embedded in client applications.</p></div><div className="dash-table-wrap"><table className="dash-table"><thead><tr><th>Name</th><th>Key</th><th>Permissions</th><th>Environment</th><th>Last used</th><th>Status</th><th /></tr></thead><tbody>{state.keys.map((key) => <tr key={key.id}><td><b>{key.name}</b></td><td><code>{key.token}</code></td><td>{key.scope}</td><td>{key.env}</td><td>{key.lastUsed}</td><td><StatusDot tone={key.active ? "healthy" : "neutral"}>{key.active ? "active" : "revoked"}</StatusDot></td><td><button className="table-action" disabled={!key.active} onClick={() => revoke(key.id)}>Revoke</button></td></tr>)}</tbody></table></div></>;
}

function Team({ state, setState, notify }: { state: PreviewState; setState: React.Dispatch<React.SetStateAction<PreviewState>>; notify: (message: string) => void }) {
  const invite = () => { const next: Member = { id: `m${Date.now()}`, name: "Invited collaborator", email: `invite${state.members.length + 1}@example.com`, initials: "IC", role: "Project viewer", scope: "Atlas", status: "Invited" }; setState((current) => ({ ...current, members: [...current.members, next] })); notify("Preview invitation created"); };
  const changeRole = (id: string, role: string) => setState((current) => ({ ...current, members: current.members.map((member) => member.id === id ? { ...member, role } : member) }));
  return <><PageHead index="08 / ORGANIZATION" title="Team & access" body="Assign the least privilege required across account, project, and environment boundaries." actions={<button className="dash-button ready" onClick={invite}>Invite member +</button>} /><div className="role-strip"><div><b>{state.members.length}</b><span>Members</span></div><div><b>1</b><span>Account owner</span></div><div><b>3</b><span>Project roles</span></div><div><b>0</b><span>Access alerts</span></div></div><div className="dash-table-wrap"><table className="dash-table team-table"><thead><tr><th>Member</th><th>Role</th><th>Scope</th><th>Status</th><th>Last active</th><th /></tr></thead><tbody>{state.members.map((member) => <tr key={member.id}><td><div className="member-cell"><i>{member.initials}</i><div><b>{member.name}</b><span>{member.email}</span></div></div></td><td><select value={member.role} disabled={member.role === "Account owner"} onChange={(event) => changeRole(member.id, event.target.value)}><option>Account owner</option><option>Project admin</option><option>Project editor</option><option>Project viewer</option></select></td><td>{member.scope}</td><td><StatusDot tone={member.status === "Active" ? "healthy" : "warning"}>{member.status}</StatusDot></td><td>{member.status === "Active" ? "Today" : "—"}</td><td><button className="row-open">•••</button></td></tr>)}</tbody></table></div><section className="access-matrix"><div><span>ROLE MATRIX</span><h2>Access remains explicit.</h2><p>Account owners manage billing and organization controls. Project administrators manage keys and data within assigned projects. Viewers can inspect without changing state.</p></div><div className="matrix-grid"><span /><b>Data</b><b>Keys</b><b>Members</b><b>Billing</b><span>Owner</span><i>✓</i><i>✓</i><i>✓</i><i>✓</i><span>Project admin</span><i>✓</i><i>✓</i><i>✓</i><i>—</i><span>Viewer</span><i>View</i><i>—</i><i>—</i><i>—</i></div></section></>;
}

function Usage() {
  return <><PageHead index="09 / USAGE" title="Usage & limits" body="Understand operation volume, storage growth, and the keys driving consumption before limits become incidents." actions={<button className="dash-button secondary">Download CSV</button>} /><div className="usage-hero"><div><span>CURRENT PERIOD · JUL 01—31</span><h2>61% of preview allocation used.</h2><p>Usage is tracking 8% below the projected monthly envelope.</p><div className="limit-track"><i style={{ width: "61%" }} /></div><div className="limit-labels"><span>0</span><b>610K / 1M operations</b><span>1M</span></div></div><div className="usage-cost"><span>ESTIMATED</span><b>$184.20</b><small>Preview estimate · not an invoice</small></div></div><div className="usage-grid"><article className="dash-panel"><div className="dash-panel-head"><div><span>Operations</span><h2>Daily volume</h2></div><span className="dash-faint">610,482 total</span></div><Bars values={usageBars} label="Daily operation volume" /><div className="chart-axis"><span>Jul 01</span><span>Jul 08</span><span>Jul 15</span></div></article><article className="dash-panel"><div className="dash-panel-head"><div><span>Allocation</span><h2>By operation</h2></div></div><div className="operation-list"><div><span>Retrieval</span><b>421,800 · 69%</b><i><em style={{ width: "69%" }} /></i></div><div><span>Ingestion</span><b>152,620 · 25%</b><i><em style={{ width: "25%" }} /></i></div><div><span>Updates</span><b>30,524 · 5%</b><i><em style={{ width: "5%" }} /></i></div><div><span>Deletes</span><b>5,538 · 1%</b><i><em style={{ width: "1%" }} /></i></div></div></article></div><div className="dash-grid-lower"><article className="dash-panel"><div className="dash-panel-head"><div><span>Storage</span><h2>Canonical data</h2></div><b>38.4 GB</b></div><div className="storage-stack"><i style={{ width: "58%" }} /><i style={{ width: "26%" }} /><i style={{ width: "16%" }} /></div><div className="storage-legend"><span><i />Source records · 22.3 GB</span><span><i />Indexes · 10.0 GB</span><span><i />Metadata · 6.1 GB</span></div></article><article className="dash-panel"><div className="dash-panel-head"><div><span>Keys</span><h2>Highest usage</h2></div></div><div className="key-usage"><div><b>production-agent</b><span>78%</span></div><div><b>support-worker</b><span>14%</span></div><div><b>eval-runner</b><span>8%</span></div></div></article></div></>;
}

function Settings({ project, environment, notify }: { project: string; environment: string; notify: (message: string) => void }) {
  const [tab, setTab] = useState("project");
  const [retention, setRetention] = useState("365 days");
  return <><PageHead index="10 / CONFIGURE" title="Settings" body="Control the project boundary, retention, retrieval defaults, security, and outbound events." actions={<button className="dash-button ready" onClick={() => notify("Preview settings saved")}>Save changes</button>} /><div className="settings-layout"><nav aria-label="Settings sections">{["project", "retrieval", "retention", "webhooks", "security", "audit"].map((item) => <button key={item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>{item}</button>)}</nav><section className="settings-content"><div className="settings-heading"><span>{tab.toUpperCase()}</span><h2>{tab === "project" ? "Project configuration" : `${tab[0].toUpperCase()}${tab.slice(1)} controls`}</h2><p>Changes in this functional preview are session-scoped and never affect live infrastructure.</p></div>{tab === "project" ? <div className="settings-form"><label><span>Project name</span><input defaultValue={project} /></label><label><span>Environment</span><input value={environment} readOnly /></label><label className="full"><span>Description</span><textarea defaultValue="Primary managed memory project for production agents and evaluation workflows." rows={4} /></label><div className="setting-toggle full"><div><b>Source retention</b><span>Require every interpretation to remain linked to its canonical source.</span></div><input type="checkbox" defaultChecked aria-label="Source retention" /></div><div className="setting-toggle full"><div><b>Strict scope isolation</b><span>Prevent cross-scope retrieval unless explicitly permitted.</span></div><input type="checkbox" defaultChecked aria-label="Strict scope isolation" /></div></div> : tab === "retention" ? <div className="settings-form"><label className="full"><span>Record retention</span><select value={retention} onChange={(event) => setRetention(event.target.value)}><option>90 days</option><option>365 days</option><option>Indefinite</option></select></label><div className="setting-toggle full"><div><b>Archive before deletion</b><span>Create an exportable archive before a retention policy removes records.</span></div><input type="checkbox" defaultChecked aria-label="Archive before deletion" /></div><div className="danger-zone full"><span>DANGER ZONE</span><h3>Delete preview environment</h3><p>This demonstration never removes live data.</p><button onClick={() => notify("Destructive action blocked in preview")}>Delete environment</button></div></div> : <div className="settings-placeholder"><span>CONFIGURATION PREVIEW</span><h3>{tab} is ready for enterprise policy.</h3><p>Configure defaults, delivery destinations, permissions, and audit visibility from this project boundary.</p><div className="setting-toggle"><div><b>Enable {tab}</b><span>Apply the recommended NarratorDB configuration.</span></div><input type="checkbox" defaultChecked aria-label={`Enable ${tab}`} /></div><div className="setting-toggle"><div><b>Notify on changes</b><span>Write every change to the organization audit log.</span></div><input type="checkbox" defaultChecked aria-label={`Notify on ${tab} changes`} /></div></div>}</section></div></>;
}
