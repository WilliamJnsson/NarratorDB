import type { Metadata } from "next";
import { ClosingCta, HeroStat, MarketingShell, PageHero, PageSection } from "../components";
import { PricingExplorer } from "../interactions";

export const metadata: Metadata = {
  title: "Pricing — NarratorDB",
  description: "Preview pricing for NarratorDB proprietary cloud memory infrastructure.",
  alternates: { canonical: "/pricing" },
};

export default function PricingPage() {
  const hero = <PageHero eyebrow="Pricing · preview targets" title="Start focused. Scale the boundary deliberately." lede="Four cloud tracks for individual experiments, production teams, and organizations planning a governed memory layer." aside={<HeroStat label="CLOUD WAITLIST" value="Open" detail="No commitment · preview planning" tone="ready" />} />;
  return <MarketingShell hero={hero}>
    <PageSection id="plans" label="01 · CLOUD PLANS" title="Clear starting points before general availability." body="The private preview will validate limits, support expectations, and deployment needs before final packages become service commitments.">
      <PricingExplorer />
    </PageSection>
    <PageSection id="principles" label="02 · PACKAGING PRINCIPLES" title="Pay for the operating boundary, not hidden retrieval tax." body="Preview packaging separates stored records, write volume, projects, support, and organization controls so teams can reason about cost." tone="acid">
      <div className="enterprise-grid three-up"><article><span>PREDICTABLE</span><h3>Retrieval stays unmetered.</h3><p>Cloud plans are designed around durable state and ingestion rather than penalizing every context lookup.</p></article><article><span>VISIBLE</span><h3>Limits stay explicit.</h3><p>Project, record, and write allowances remain visible before they become enforced service boundaries.</p></article><article><span>ADAPTIVE</span><h3>Enterprise starts with requirements.</h3><p>Governance, retention, region, and private deployment needs shape the commercial plan.</p></article></div>
    </PageSection>
    <ClosingCta title="Choose the cloud track that matches your boundary." />
  </MarketingShell>;
}
