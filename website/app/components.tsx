import Link from "next/link";
import type { ReactNode } from "react";
import { MotionController, SectionNav } from "./site-client";
import { SummarizeWithAI } from "./summarize-with-ai";

export type SectionLink = { id: string; label: string; number: string };

export function SiteHeader() {
  const links = [["Product", "/product"], ["Research", "/research"], ["Pricing", "/pricing"]];
  return <header className="site-header">
    <div className="shell header-inner">
      <Link className="wordmark" href="/" aria-label="NarratorDB home"><span>N</span>NarratorDB</Link>
      <nav className="desktop-nav" aria-label="Primary navigation">{links.map(([label, href]) => <Link href={href} key={href}>{label}</Link>)}</nav>
      <Link className="header-action" href="/early-access">Request access <span>↗</span></Link>
      <details className="mobile-nav"><summary>Menu</summary><nav>{links.map(([label, href]) => <Link href={href} key={href}>{label}</Link>)}<Link href="/early-access">Request access</Link></nav></details>
    </div>
  </header>;
}

export function SiteFooter() {
  return <footer className="site-footer shell">
    <div><Link className="wordmark" href="/"><span>N</span>NarratorDB</Link><p>Proprietary cloud memory infrastructure for AI systems.</p></div>
    <nav aria-label="Footer navigation"><Link href="/product">Product</Link><Link href="/research">Research</Link><Link href="/pricing">Pricing</Link><Link href="/early-access">Cloud access</Link></nav>
    <div className="footer-meta"><span>NarratorDB Cloud · Private preview</span><Link href="/early-access#privacy">Privacy notice</Link></div>
  </footer>;
}

export function MarketingShell({ children, sections, hero }: { children: ReactNode; sections?: SectionLink[]; hero?: ReactNode }) {
  return <main><SiteHeader />{hero}{sections?.length ? <SectionNav sections={sections} /> : null}{children}<SiteFooter /><SummarizeWithAI /><MotionController /></main>;
}

export function Eyebrow({ children, tone = "ready" }: { children: ReactNode; tone?: "ready" | "neutral" }) {
  return <div className={`eyebrow ${tone}`}><i />{children}</div>;
}

export function SectionHeading({ label, title, body }: { label: string; title: string; body: string }) {
  return <div className="section-heading"><span>{label}</span><h2>{title}</h2><p>{body}</p></div>;
}

export function PageSection({ id, label, title, body, children, surface = false, tone = "white" }: { id: string; label: string; title: string; body: string; children: ReactNode; surface?: boolean; tone?: "white" | "silver" | "acid" | "pink" | "coral" | "ink" }) {
  const resolvedTone = surface && tone === "white" ? "silver" : tone;
  return <section className={`section-band tone-${resolvedTone}`} id={id} data-reveal><div className="shell"><SectionHeading label={label} title={title} body={body} />{children}</div></section>;
}

export function PageHero({ eyebrow, title, lede, aside }: { eyebrow: string; title: string; lede: string; aside?: ReactNode }) {
  return <section className="page-hero shell" data-reveal><div><Eyebrow>{eyebrow}</Eyebrow><h1>{title}</h1><p>{lede}</p></div>{aside && <div className="page-hero-aside">{aside}</div>}</section>;
}

export function HeroStat({ label, value, detail, tone = "neutral" }: { label: string; value: string; detail: string; tone?: "neutral" | "ready" | "research" }) {
  return <div className={`hero-stat ${tone}`}><span>{label}</span><b>{value}</b><small>{detail}</small></div>;
}

export function ClosingCta({ title = "Bring durable memory into the cloud.", body = "Join the private preview from a personal project to an enterprise deployment." }: { title?: string; body?: string }) {
  return <section className="closing shell" data-reveal><div><span>CLOUD ACCESS</span><h2>{title}</h2><p>{body}</p></div><Link className="button primary" href="/early-access">Request access <span>↗</span></Link></section>;
}

export function RetrievalDemo() {
  return <div className="retrieval-demo" aria-label="NarratorDB cloud retrieval example">
    <div className="demo-head"><span><i /> project / production</span><b>CLOUD · HEALTHY</b></div>
    <div className="demo-query"><span>QUERY</span><p>What passed the durability suite?</p></div>
    <div className="demo-result"><div><span>TOP MATCH · 0.94</span><b>The release candidate passed the durability suite.</b></div><small>source attached · scoped retrieval</small></div>
    <div className="demo-flow"><span>ingest source</span><i /><span>rank evidence</span><i /><span>return context</span></div>
  </div>;
}

export function StatusBadge({ children, tone = "default" }: { children: ReactNode; tone?: "default" | "available" | "planned" | "research" }) {
  return <span className={`status-badge ${tone}`}>{children}</span>;
}
