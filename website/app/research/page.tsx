import type { Metadata } from "next";
import { ClosingCta, HeroStat, MarketingShell, PageHero, PageSection, StatusBadge, type SectionLink } from "../components";
import { CategoryComparison, EfficiencyPlot, ResearchWorkbench } from "../research-charts";
import { providerProfiles, validateResearchData } from "../research-data";

export const metadata: Metadata = {
  title: "Research — NarratorDB",
  description: "Sourced AI memory benchmarks and provider research with the methodology attached.",
  alternates: { canonical: "/research" },
};

const sections: SectionLink[] = [
  { id: "results", label: "Results", number: "01" },
  { id: "question-types", label: "Question types", number: "02" },
  { id: "efficiency", label: "Efficiency", number: "03" },
  { id: "methodology", label: "Methodology", number: "04" },
  { id: "providers", label: "Providers", number: "05" },
];

export default function ResearchPage() {
  const researchProblems = validateResearchData();
  if (researchProblems.length) throw new Error(`Invalid research data: ${researchProblems.join(", ")}`);
  const hero = <PageHero eyebrow="Research · verified 2026-07-15" title="Compare the evidence, not the headline." lede="A detailed view of AI memory systems, their published scores, architectures, and the methodological choices that make numbers comparable—or not." aside={<HeroStat label="FROZEN LONGMEMEVAL" value="82.8%" detail="97.4% evidence coverage · top 50" tone="research" />} />;
  return <MarketingShell hero={hero} sections={sections}>
    <PageSection id="results" label="01 · REPORTED RESULTS" title="One benchmark at a time." body="Every mark carries its score definition, model setup, cutoff, source, and verification date. Different configurations remain visibly different.">
      <ResearchWorkbench />
    </PageSection>

    <PageSection id="question-types" label="02 · QUESTION TYPES" title="Overall scores hide where memory breaks." body="Switch between providers while keeping all six LongMemEval question types visible on the same scale." tone="white">
      <CategoryComparison />
    </PageSection>

    <PageSection id="efficiency" label="03 · EFFICIENCY" title="Accuracy is only one production constraint." body="Published median latency is sparse and crosses local and managed infrastructure. The chart shows what exists without pretending the conditions match." tone="silver">
      <EfficiencyPlot />
    </PageSection>

    <PageSection id="methodology" label="04 · METHODOLOGY" title="The configuration is part of the result." body="A benchmark number is incomplete without the system boundary and evaluation path that produced it." tone="acid">
      <div className="methodology-grid"><article><span>DATASET</span><h3>Which questions?</h3><p>Dataset variant, question count, failed-sample policy, and development/holdout boundary.</p></article><article><span>RETRIEVAL</span><h3>How much context?</h3><p>Top-k depth, aggregation, reranking, result shaping, and tokens delivered to the reader.</p></article><article><span>MODELS</span><h3>Who answered and judged?</h3><p>Reader, judge, prompts, reasoning settings, and provider routing can materially move accuracy.</p></article><article><span>INFRASTRUCTURE</span><h3>Where did it run?</h3><p>Local and managed latency can be useful without becoming a controlled head-to-head.</p></article></div>
      <div className="methodology-note"><p>NarratorDB does not claim controlled superiority until dataset, retrieval budget, reader, judge, prompts, infrastructure, retry policy, and denominator are pinned together.</p><a href="/research/narratordb-longmemeval-2026-07-15.json">Download the sanitized first-party record ↗</a></div>
    </PageSection>

    <PageSection id="providers" label="05 · PROVIDERS" title="The market, with the missing fields left visible." body="Begin with the compact index, then open only the provider records needed for architecture, deployment, licensing, and public evidence review.">
      <div className="provider-index" aria-label="Provider index">{providerProfiles.map((provider) => <a href={`#provider-${provider.id}`} key={provider.id}><span>{provider.kind}</span><b>{provider.name}</b></a>)}</div>
      <div className="provider-directory">{providerProfiles.map((provider, index) => <details className="provider-profile" id={`provider-${provider.id}`} open={index === 0} key={provider.id}><summary><div><StatusBadge tone="research">VERIFIED</StatusBadge><span>{provider.kind}</span><h3>{provider.name}</h3></div><p>{provider.summary}</p><i aria-hidden="true">+</i></summary><div className="provider-detail"><div className="provider-source"><small>{`Source verified ${provider.source.verified}`}</small><a href={provider.source.url} target="_blank" rel="noreferrer">Primary source ↗</a></div><dl><div><dt>Architecture</dt><dd>{provider.architecture}</dd></div><div><dt>Retrieval</dt><dd>{provider.retrieval}</dd></div><div><dt>Temporal model</dt><dd>{provider.temporal}</dd></div><div><dt>Deployment</dt><dd>{provider.deployment}</dd></div><div><dt>License</dt><dd>{provider.license}</dd></div></dl><div className="provider-lists"><div><span>PUBLIC STRENGTHS</span><ul>{provider.strengths.map((item) => <li key={item}>{item}</li>)}</ul></div><div><span>EVIDENCE GAPS</span><ul>{provider.publicGaps.map((item) => <li key={item}>{item}</li>)}</ul></div></div></div></details>)}</div>
    </PageSection>
    <ClosingCta title="Build against evidence, not a leaderboard screenshot." body="Join the cloud preview and help shape the next controlled comparison." />
  </MarketingShell>;
}
