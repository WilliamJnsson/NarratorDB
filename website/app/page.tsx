import Link from "next/link";
import { ClosingCta, Eyebrow, MarketingShell, PageSection, RetrievalDemo, StatusBadge } from "./components";
import { HeroMemoryField } from "./hero-memory-field";
import { DashboardShowcase } from "./dashboard-showcase";
import { MemoryShowcase } from "./memory-showcase";
import { ResearchWorkbench } from "./research-charts";

export const metadata = {
  title: "NarratorDB — Cloud memory infrastructure for AI",
  description: "Proprietary cloud memory with canonical records, transparent retrieval, explicit provenance, and reproducible research.",
};

const proof = [
  ["19.05 ms", "historical p50 search"],
  ["97.4%", "evidence in context"],
  ["245,780", "messages in frozen run"],
  ["500 / 500", "questions completed"],
];

export default function Home() {
  return <MarketingShell>
    <section className="home-hero shell" data-reveal>
      <HeroMemoryField />
      <div className="home-copy">
        <span className="hero-kicker">PRIVATE CLOUD · ACCESS OPEN</span>
        <h1>Memory infrastructure that keeps the source.</h1>
        <p>Canonical records, transparent retrieval, and operational control for agents that need to remember across every session.</p>
        <div className="hero-actions"><Link className="button primary" href="/early-access">Request cloud access <span>↗</span></Link><Link className="button secondary" href="/research">Review the evidence <span>↓</span></Link></div>
      </div>
    </section>

    <section className="status-strip shell" aria-label="NarratorDB cloud status" data-reveal>
      <div><StatusBadge tone="available">WAITLIST OPEN</StatusBadge><span>Personal projects to enterprise systems</span></div>
      <div><StatusBadge tone="research">VERIFIED</StatusBadge><span>Frozen research record · 2026-07-15</span></div>
      <div><StatusBadge tone="planned">PRIVATE PREVIEW</StatusBadge><span>Managed cloud access in controlled rollout</span></div>
    </section>

    <PageSection id="platform" label="01 · PLATFORM" title="A durable memory layer beneath every agent." body="NarratorDB Cloud separates the canonical record from model interpretation, then gives applications one controlled path to ingest, retrieve, and audit context." tone="acid">
      <div className="enterprise-grid three-up">
        <article><span>01 / INGEST</span><h3>Keep the original record.</h3><p>Conversations, events, artifacts, and relations remain traceable to the source that produced them.</p></article>
        <article><span>02 / RETRIEVE</span><h3>Rank bounded evidence.</h3><p>Lexical, semantic, temporal, and provenance signals produce context without hiding the retrieval path.</p></article>
        <article><span>03 / OPERATE</span><h3>Make memory observable.</h3><p>Project isolation, health reporting, retention, and deployment controls turn memory into infrastructure.</p></article>
      </div>
      <Link className="text-link" href="/product">Explore the cloud platform <span>↗</span></Link>
    </PageSection>

    <PageSection id="workflow" label="02 · WORKFLOW" title="From application event to inspectable context." body="The product boundary stays narrow: your application sends source material, NarratorDB maintains the memory system, and retrieval returns evidence with provenance attached." tone="silver">
      <div className="split-feature"><div><Eyebrow>Cloud project · healthy</Eyebrow><h3>One managed boundary, without an opaque memory layer.</h3><p>Connect through application APIs and agent protocols while preserving the record, scope, and retrieval configuration behind each answer.</p><ul className="plain-list"><li>Project and environment isolation</li><li>Source-first ingestion</li><li>Inspectable retrieval configuration</li><li>Operational health and audit trail</li></ul></div><RetrievalDemo /></div>
    </PageSection>

    <PageSection id="showcase" label="03 · LIVE SHOWCASE" title="See memory move from conversation to evidence." body="Three products, one controlled pipeline. Watch NarratorDB preserve the source, build scoped memory, and return traceable context." tone="white">
      <MemoryShowcase />
    </PageSection>

    <PageSection id="control-plane" label="04 · CONTROL PLANE" title="Operate memory like infrastructure." body="Inspect every record, test every retrieval, and follow production health from one clear operating surface." tone="silver">
      <DashboardShowcase />
      <Link className="text-link" href="/early-access">Request dashboard access <span>↗</span></Link>
    </PageSection>

    <section className="proof-band shell" aria-label="Frozen NarratorDB benchmark summary" data-reveal>{proof.map(([value, label]) => <div key={label}><b>{value}</b><span>{label}</span></div>)}</section>

    <PageSection id="research" label="05 · RESEARCH" title="Published numbers, with their rules attached." body="Compare memory systems without hiding the model, cutoff, token budget, infrastructure, or benchmark behind the score." tone="white">
      <ResearchWorkbench compact />
      <Link className="text-link" href="/research">Open the complete research record <span>↗</span></Link>
    </PageSection>

    <PageSection id="reliability" label="06 · RELIABILITY" title="Memory is state. Treat it like infrastructure." body="A useful memory layer survives contention, cleanup, migration, and the moment an enterprise team needs to prove where context came from." tone="ink">
      <div className="reliability-list">
        <article><span>01</span><div><h3>Explicit project boundaries</h3><p>Every query, record, and integration belongs to a named cloud project and environment.</p></div></article>
        <article><span>02</span><div><h3>Traceable context</h3><p>Retrieved context stays connected to source records, ranking signals, and the selected scope.</p></div></article>
        <article><span>03</span><div><h3>Controlled lifecycle</h3><p>Retention, cleanup, migration, and health checks remain part of the product boundary.</p></div></article>
      </div>
    </PageSection>

    <PageSection id="access" label="07 · ACCESS" title="Start small. Keep the same memory discipline as you scale." body="Cloud preview tracks are designed for personal experiments, production teams, and enterprise deployment planning." tone="coral">
      <div className="access-track-grid"><article><span>FREE</span><h3>Personal</h3><p>Explore durable cloud memory with a focused project and a clear usage boundary.</p></article><article><span>BUILDER</span><h3>Teams</h3><p>Move multiple agents into a shared project with higher ingestion limits and support.</p></article><article><span>ENTERPRISE</span><h3>Organizations</h3><p>Plan governance, private boundaries, migration, and operational requirements.</p></article></div>
      <Link className="text-link" href="/pricing">Review preview pricing <span>↗</span></Link>
    </PageSection>
    <ClosingCta />
  </MarketingShell>;
}
