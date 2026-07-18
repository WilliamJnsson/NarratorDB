import type { Metadata } from "next";
import { ClosingCta, HeroStat, MarketingShell, PageHero, PageSection, StatusBadge, type SectionLink } from "../components";

export const metadata: Metadata = {
  title: "Product — NarratorDB",
  description: "NarratorDB proprietary cloud memory architecture, interfaces, reliability, and deployment direction.",
  alternates: { canonical: "/product" },
};

const sections: SectionLink[] = [
  { id: "principles", label: "Principles", number: "01" },
  { id: "architecture", label: "Architecture", number: "02" },
  { id: "interfaces", label: "Interfaces", number: "03" },
  { id: "reliability", label: "Reliability", number: "04" },
  { id: "deployment", label: "Deployment", number: "05" },
  { id: "use-cases", label: "Use cases", number: "06" },
];

const interfaces = [
  ["Python SDK", "Managed project, ingestion, retrieval, and operational APIs for application services.", "READY"],
  ["MCP gateway", "Scoped memory tools for MCP-compatible agents and developer environments.", "READY"],
  ["HTTP API", "Language-neutral ingestion and retrieval across cloud project boundaries.", "READY"],
  ["Batch ingestion", "Controlled import paths for conversations, records, and historical context.", "PREVIEW"],
  ["LangGraph", "First-party memory adapters for stateful graph workflows.", "PLANNED"],
  ["Agents SDK", "Persistent memory hooks for agent runs, tools, and handoffs.", "PLANNED"],
];

export default function ProductPage() {
  const hero = <PageHero eyebrow="Product · proprietary cloud" title="A memory system your team can inspect." lede="Canonical records, transparent retrieval, explicit scope, and operational controls behind one managed boundary." aside={<HeroStat label="CLOUD ACCESS" value="Open" detail="Personal · team · enterprise" tone="ready" />} />;
  return <MarketingShell hero={hero} sections={sections}>
    <PageSection id="principles" label="01 · PRINCIPLES" title="Keep the record separate from the interpretation." body="Models and frameworks will change. The durable source should remain explicit, traceable, and testable beneath them.">
      <div className="enterprise-grid three-up"><article><span>CANONICAL</span><h3>Original records survive.</h3><p>Derived indexes and typed structures remain connected to the source that produced them.</p></article><article><span>SCOPED</span><h3>Isolation is the default.</h3><p>Retrieval runs inside an explicit project, environment, and logical application boundary.</p></article><article><span>OBSERVABLE</span><h3>Health has an answer.</h3><p>Teams can reason about ingestion, retrieval configuration, lifecycle, and operational state.</p></article></div>
    </PageSection>

    <PageSection id="architecture" label="02 · ARCHITECTURE" title="One clear path from event to evidence." body="NarratorDB Cloud owns the memory lifecycle while keeping the source record and retrieval path visible to the application." tone="acid">
      <div className="architecture-flow"><div><span>01</span><b>Ingest source</b><p>Conversation, record, artifact, or event</p></div><i /><div><span>02</span><b>Build memory</b><p>Indexes, temporal signals, and relations</p></div><i /><div><span>03</span><b>Rank context</b><p>Intent, scope, time, and provenance</p></div><i /><div><span>04</span><b>Return evidence</b><p>Bounded context with traceable metadata</p></div></div>
    </PageSection>

    <PageSection id="interfaces" label="03 · INTERFACES" title="A narrow cloud boundary for every stack." body="Use application APIs, agent protocols, or batch ingestion without turning memory into framework-specific state.">
      <div className="interface-list">{interfaces.map(([name, body, state]) => <article key={name}><div><h3>{name}</h3><StatusBadge tone={state === "READY" ? "available" : state === "PLANNED" ? "planned" : "default"}>{state}</StatusBadge></div><p>{body}</p></article>)}</div>
    </PageSection>

    <PageSection id="reliability" label="04 · RELIABILITY" title="Operational discipline is part of memory quality." body="Retrieval accuracy matters only when the underlying record survives real production behavior." tone="ink">
      <div className="durability-grid"><article><b>Committed ingestion</b><p>Every accepted record crosses a defined durability boundary before the operation completes.</p></article><article><b>Recoverable state</b><p>Backups, migrations, and integrity checks remain part of the managed system.</p></article><article><b>Repairable indexes</b><p>Derived structures can be rebuilt without replacing or obscuring the canonical record.</p></article><article><b>Complete cleanup</b><p>Retention policies remove indexes, provenance, and relations with the source lifecycle.</p></article></div>
    </PageSection>

    <PageSection id="deployment" label="05 · DEPLOYMENT" title="Start managed. Expand the boundary deliberately." body="The private preview begins in the managed cloud. Enterprise deployment options remain explicit roadmap items rather than implied commitments." tone="pink">
      <div className="deployment-row"><article><StatusBadge tone="available">WAITLIST OPEN</StatusBadge><h3>Managed cloud</h3><p>Projects, environments, ingestion, retrieval, and operational visibility in one service boundary.</p></article><article><StatusBadge>PRIVATE PREVIEW</StatusBadge><h3>Team controls</h3><p>Shared projects, expanded limits, support, and deployment planning during controlled rollout.</p></article><article><StatusBadge tone="planned">PLANNED</StatusBadge><h3>Private boundary</h3><p>Commercial infrastructure designed around organization and cloud requirements.</p></article></div>
    </PageSection>

    <PageSection id="use-cases" label="06 · USE CASES" title="The same memory discipline across products." body="Support, sales, care, and developer agents all need durable scope, source preservation, and evidence that survives the next session." tone="coral">
      <div className="use-case-line"><span>Customer support</span><span>Sales & CRM</span><span>Healthcare workflows</span><span>Developer agents</span></div>
    </PageSection>
    <ClosingCta title="Give every agent a durable cloud memory boundary." />
  </MarketingShell>;
}
