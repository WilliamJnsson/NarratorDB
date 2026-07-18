"use client";

import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

type StageId = "ingest" | "build" | "retrieve";

type MemoryRecord = {
  label: string;
  value: string;
};

type ShowcaseScenario = {
  id: "personal" | "care" | "support";
  tab: string;
  context: string;
  source: string;
  sourceMeta: string;
  records: MemoryRecord[];
  query: string;
  answer: string;
  evidence: string;
  scope: string;
};

const scenarios: ShowcaseScenario[] = [
  {
    id: "personal",
    tab: "Personal assistant",
    context: "Personal workspace",
    source: "I work from cafés on Fridays, prefer a strong flat white, and start deep work around 10 AM.",
    sourceMeta: "conversation · user preference · now",
    records: [
      { label: "PLACE", value: "Café on Fridays" },
      { label: "PREFERENCE", value: "Strong flat white" },
      { label: "ROUTINE", value: "Deep work · 10 AM" },
    ],
    query: "Plan my Friday morning.",
    answer: "Start at a café around 10 AM, with time for a strong flat white before deep work.",
    evidence: "3 source-linked records",
    scope: "personal / friday-routine",
  },
  {
    id: "care",
    tab: "Care coordination",
    context: "Fictional care workspace",
    source: "Jordan prefers morning appointments, needs a Swedish interpreter, and Dr. Shah owns the July 22 follow-up.",
    sourceMeta: "care coordination · fictional record · now",
    records: [
      { label: "SCHEDULE", value: "Morning appointment" },
      { label: "ACCESS", value: "Swedish interpreter" },
      { label: "OWNER", value: "Dr. Shah · July 22" },
    ],
    query: "Set up Jordan’s follow-up.",
    answer: "Schedule the July 22 follow-up in the morning, request a Swedish interpreter, and keep Dr. Shah as owner.",
    evidence: "3 scoped coordination records",
    scope: "care / coordination-only",
  },
  {
    id: "support",
    tab: "Customer support",
    context: "Support workspace",
    source: "Northstar runs in EU-West. Incident INC-482 was resolved by rotating the webhook key. Mina owns the follow-up.",
    sourceMeta: "support thread · incident record · now",
    records: [
      { label: "ENVIRONMENT", value: "Northstar · EU-West" },
      { label: "RESOLUTION", value: "Rotate webhook key" },
      { label: "OWNER", value: "Mina · INC-482" },
    ],
    query: "What fixed Northstar’s last webhook incident?",
    answer: "INC-482 was resolved by rotating the webhook key. Mina owns the follow-up in EU-West.",
    evidence: "incident + environment + owner",
    scope: "support / northstar",
  },
];

const stages: { id: StageId; number: string; label: string; detail: string; start: number; end: number }[] = [
  { id: "ingest", number: "01", label: "Ingest", detail: "Commit the original source", start: 0, end: 6000 },
  { id: "build", number: "02", label: "Build", detail: "Create scoped memory", start: 6000, end: 11600 },
  { id: "retrieve", number: "03", label: "Retrieve", detail: "Return traceable evidence", start: 11600, end: 24600 },
];

const cycleDuration = 27000;
const completeTime = 24000;

function stageAt(time: number): StageId {
  if (time < stages[1].start) return "ingest";
  if (time < stages[2].start) return "build";
  return "retrieve";
}

function typedText(text: string, time: number, start: number, duration: number) {
  const progress = Math.max(0, Math.min(1, (time - start) / duration));
  return text.slice(0, Math.round(text.length * progress));
}

function fullStageTime(stage: StageId) {
  if (stage === "ingest") return stages[0].end - 150;
  if (stage === "build") return stages[1].end - 150;
  return completeTime;
}

export function MemoryShowcase() {
  const rootRef = useRef<HTMLDivElement>(null);
  const elapsedRef = useRef(0);
  const scenarioRef = useRef(0);
  const visibleRef = useRef(false);
  const lastFrameRef = useRef<number | null>(null);
  const lastRenderRef = useRef(0);
  const reducedRef = useRef(false);
  const scenarioTabsRef = useRef<HTMLDivElement>(null);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const [scenarioIndex, setScenarioIndex] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [reducedMotion, setReducedMotion] = useState(false);

  const scenario = scenarios[scenarioIndex];
  const activeStage = stageAt(elapsed);

  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const applyMotionPreference = () => {
      reducedRef.current = media.matches;
      setReducedMotion(media.matches);
      if (media.matches) {
        elapsedRef.current = completeTime;
        setElapsed(completeTime);
      }
      lastFrameRef.current = null;
    };
    applyMotionPreference();
    media.addEventListener("change", applyMotionPreference);
    return () => media.removeEventListener("change", applyMotionPreference);
  }, []);

  useEffect(() => {
    const node = rootRef.current;
    if (!node) return;
    const observer = new IntersectionObserver(([entry]) => {
      visibleRef.current = entry.isIntersecting && entry.intersectionRatio >= .35;
      lastFrameRef.current = null;
    }, { threshold: [.15, .35, .65] });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let frame = 0;
    const tick = (now: number) => {
      const canAdvance = visibleRef.current && document.visibilityState === "visible" && !reducedRef.current;
      if (canAdvance) {
        const previous = lastFrameRef.current ?? now;
        const next = elapsedRef.current + Math.min(now - previous, 80);
        if (next >= cycleDuration) {
          const nextScenario = (scenarioRef.current + 1) % scenarios.length;
          scenarioRef.current = nextScenario;
          elapsedRef.current = 0;
          setScenarioIndex(nextScenario);
          setElapsed(0);
        } else {
          elapsedRef.current = next;
          if (now - lastRenderRef.current >= 32) {
            lastRenderRef.current = now;
            setElapsed(next);
          }
        }
        lastFrameRef.current = now;
      } else {
        lastFrameRef.current = null;
      }
      frame = window.requestAnimationFrame(tick);
    };
    frame = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    const tabs = scenarioTabsRef.current;
    const tab = tabRefs.current[scenarioIndex];
    if (!tabs || !tab || tabs.scrollWidth <= tabs.clientWidth) return;
    tabs.scrollTo({
      left: tab.offsetLeft - (tabs.clientWidth - tab.offsetWidth) / 2,
      behavior: reducedRef.current ? "auto" : "smooth",
    });
  }, [scenarioIndex]);

  const selectScenario = (index: number) => {
    scenarioRef.current = index;
    setScenarioIndex(index);
    const time = reducedRef.current ? completeTime : 0;
    elapsedRef.current = time;
    setElapsed(time);
    lastFrameRef.current = null;
  };

  const selectStage = (stage: StageId) => {
    const definition = stages.find((item) => item.id === stage) ?? stages[0];
    const time = reducedRef.current ? fullStageTime(stage) : definition.start;
    elapsedRef.current = time;
    setElapsed(time);
    lastFrameRef.current = null;
  };

  const moveScenarioFocus = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let target = index;
    if (event.key === "ArrowRight") target = (index + 1) % scenarios.length;
    else if (event.key === "ArrowLeft") target = (index - 1 + scenarios.length) % scenarios.length;
    else if (event.key === "Home") target = 0;
    else if (event.key === "End") target = scenarios.length - 1;
    else return;
    event.preventDefault();
    selectScenario(target);
    window.requestAnimationFrame(() => tabRefs.current[target]?.focus());
  };

  const liveMessage = reducedMotion
    ? `${scenario.tab}, ${activeStage} stage, static preview.`
    : `${scenario.tab}, ${activeStage} stage.`;

  return <div className="memory-showcase" ref={rootRef} aria-label="Animated NarratorDB memory showcase">
    <div className="scenario-tabs" ref={scenarioTabsRef} role="tablist" aria-label="Choose a NarratorDB use case">
      {scenarios.map((item, index) => <button
        type="button"
        role="tab"
        aria-selected={scenarioIndex === index}
        aria-controls="memory-showcase-description"
        tabIndex={scenarioIndex === index ? 0 : -1}
        ref={(node) => { tabRefs.current[index] = node; }}
        className={scenarioIndex === index ? "active" : ""}
        data-scenario={item.id}
        onClick={() => selectScenario(index)}
        onKeyDown={(event) => moveScenarioFocus(event, index)}
        key={item.id}
      ><span>{String(index + 1).padStart(2, "0")}</span>{item.tab}</button>)}
    </div>

    <div className="showcase-body">
      <div className="showcase-rail" aria-label="NarratorDB memory stages">
        {stages.map((stage) => {
          const complete = elapsed >= stage.end;
          const active = activeStage === stage.id;
          return <button
            type="button"
            className={`${active ? "active" : ""} ${complete ? "complete" : ""}`.trim()}
            aria-current={active ? "step" : undefined}
            onClick={() => selectStage(stage.id)}
            key={stage.id}
          ><i aria-hidden="true" /><span>{stage.number}</span><strong>{stage.label}</strong><small>{stage.detail}</small></button>;
        })}
      </div>

      <div className="showcase-stage" data-scenario={scenario.id} data-stage={activeStage} aria-hidden="true">
        <div className="stage-topline">
          <span><i /> {scenario.context}</span>
          <b>{reducedMotion ? "STATIC PREVIEW" : "LIVE SIMULATION"}</b>
        </div>
        <div className="stage-progress" aria-hidden="true"><i style={{ width: `${Math.min(100, (elapsed / cycleDuration) * 100)}%` }} /></div>

        {activeStage === "ingest" && <IngestScene scenario={scenario} elapsed={elapsed} reduced={reducedMotion} />}
        {activeStage === "build" && <BuildScene scenario={scenario} elapsed={elapsed} reduced={reducedMotion} />}
        {activeStage === "retrieve" && <RetrieveScene scenario={scenario} elapsed={elapsed} reduced={reducedMotion} />}
      </div>
    </div>

    <p className="sr-only" id="memory-showcase-description" aria-live="polite">{elapsed >= 22500 ? `${scenario.tab} complete. ${scenario.answer}` : liveMessage}</p>
  </div>;
}

function IngestScene({ scenario, elapsed, reduced }: { scenario: ShowcaseScenario; elapsed: number; reduced: boolean }) {
  const local = reduced ? stages[0].end : elapsed;
  const message = typedText(scenario.source, local, 650, 3550);
  const typing = local >= 650 && local < 4200;
  const committed = local >= 4700;
  return <div className="showcase-scene ingest-scene" key={`${scenario.id}-ingest`}>
    <div className="scene-label"><span>01 / SOURCE EVENT</span><b>{committed ? "COMMITTED" : "RECEIVING"}</b></div>
    <div className="conversation-window">
      <div className="conversation-meta"><span>New conversation</span><small>canonical text retained</small></div>
      <div className="message-bubble user-message">
        <span>USER</span>
        <p>{message}<i className={typing ? "typing" : ""} aria-hidden="true" /></p>
      </div>
      <div className={`commit-receipt ${committed ? "visible" : ""}`}>
        <i aria-hidden="true">✓</i><div><b>Source committed</b><span>{scenario.sourceMeta}</span></div>
      </div>
    </div>
    <div className="scene-footer"><span>Original record</span><i /><span>Project scoped</span><i /><span>Ready to index</span></div>
  </div>;
}

function BuildScene({ scenario, elapsed, reduced }: { scenario: ShowcaseScenario; elapsed: number; reduced: boolean }) {
  const local = reduced ? stages[1].end : elapsed;
  return <div className="showcase-scene build-scene" key={`${scenario.id}-build`}>
    <div className="scene-label"><span>02 / MEMORY BUILD</span><b>SCOPED</b></div>
    <div className="source-card"><span>SOURCE RECORD</span><p>{scenario.source}</p><small>{scenario.sourceMeta}</small></div>
    <div className="build-status"><span>Building memory</span><i><em style={{ width: `${Math.min(100, Math.max(8, ((local - stages[1].start) / (stages[1].end - stages[1].start)) * 100))}%` }} /></i></div>
    <div className="record-grid">
      {scenario.records.map((record, index) => {
        const visible = reduced || local >= stages[1].start + 800 + index * 900;
        return <div className={`memory-record ${visible ? "visible" : ""}`} key={record.label}>
          <span><i aria-hidden="true">✓</i>{record.label}</span><b>{record.value}</b><small>source linked</small>
        </div>;
      })}
    </div>
    <div className="scene-footer"><span>Lexical</span><i /><span>Temporal</span><i /><span>Relations</span></div>
  </div>;
}

function RetrieveScene({ scenario, elapsed, reduced }: { scenario: ShowcaseScenario; elapsed: number; reduced: boolean }) {
  const local = reduced ? completeTime : elapsed;
  const query = typedText(scenario.query, local, 12100, 2600);
  const queryTyping = local >= 12100 && local < 14700;
  const evidenceVisible = local >= 16000;
  const answer = typedText(scenario.answer, local, 18100, 4400);
  const answerTyping = local >= 18100 && local < 22500;
  return <div className="showcase-scene retrieve-scene" key={`${scenario.id}-retrieve`}>
    <div className="scene-label"><span>03 / RETRIEVAL</span><b>{local >= 22500 ? "EVIDENCE RETURNED" : "RANKING"}</b></div>
    <div className="retrieve-thread">
      <div className="message-bubble query-message"><span>NEXT SESSION</span><p>{query}<i className={queryTyping ? "typing" : ""} aria-hidden="true" /></p></div>
      <div className={`evidence-stack ${evidenceVisible ? "visible" : ""}`}>
        <div><span>TOP EVIDENCE · 0.94</span><b>{scenario.evidence}</b></div>
        <small>{scenario.scope} · provenance attached</small>
      </div>
      <div className={`message-bubble answer-message ${local >= 18100 ? "visible" : ""}`}>
        <span>NARRATORDB CONTEXT</span><p>{answer}<i className={answerTyping ? "typing" : ""} aria-hidden="true" /></p>
      </div>
    </div>
    <div className="scene-footer"><span>Intent ranked</span><i /><span>Scope checked</span><i /><span>Source attached</span></div>
  </div>;
}
