"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";

const plans = [
  { name: "Free", monthly: 0, note: "A focused cloud project for individual exploration.", detail: ["1 cloud project", "10k records", "Core ingestion and retrieval", "Community support"], state: "WAITLIST OPEN" },
  { name: "Builder", monthly: 15, note: "Higher limits for prototypes and small production agents.", detail: ["3 cloud projects", "250k records", "100k writes / month", "Email support"], state: "PRIVATE PREVIEW" },
  { name: "Pro", monthly: 59, note: "Shared memory infrastructure for teams moving into production.", detail: ["10 cloud projects", "1m records", "500k writes / month", "Priority support"], state: "PRIVATE PREVIEW" },
] as const;

type Audience = "personal" | "team" | "enterprise";
type SubmitState = "idle" | "submitting" | "success" | "error";

export function PricingExplorer() {
  const [annual, setAnnual] = useState(false);
  return <div className="pricing-explorer">
    <div className="billing-switch" aria-label="Billing cadence"><button type="button" aria-pressed={!annual} onClick={() => setAnnual(false)}>Monthly</button><button type="button" aria-pressed={annual} onClick={() => setAnnual(true)}>Annual · save 20%</button></div>
    <div className="pricing-grid">{plans.map((plan) => {
      const price = annual ? Math.round(plan.monthly * .8) : plan.monthly;
      return <article className="price-plan" key={plan.name}><div><span>{plan.state}</span><h2>{plan.name}</h2><p>{plan.note}</p></div><div className="plan-price"><b>${price}</b><span>/ month</span></div><ul>{plan.detail.map((item) => <li key={item}>{item}</li>)}</ul><Link className={`button ${plan.name === "Pro" ? "primary" : "secondary"}`} href={`/early-access?tier=${plan.name.toLowerCase()}`}>Request access <span>↗</span></Link></article>;
    })}<article className="price-plan enterprise"><div><span>PLANNED</span><h2>Enterprise</h2><p>Governance and deployment planning around your organization boundary.</p></div><div className="plan-price"><b>Custom</b></div><ul><li>Organization controls</li><li>SSO and audit exports</li><li>Retention requirements</li><li>Private-boundary planning</li></ul><Link className="button secondary" href="/early-access?tier=enterprise">Discuss requirements <span>↗</span></Link></article></div>
    <p className="pricing-caveat">Prices and limits are preview targets for NarratorDB Cloud, not current service commitments. Final packaging may change before general availability.</p>
  </div>;
}

export function EarlyAccessForm() {
  const [audience, setAudience] = useState<Audience>("team");
  const [state, setState] = useState<SubmitState>("idle");
  const [error, setError] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setState("submitting");
    const form = new FormData(event.currentTarget);
    const body = Object.fromEntries(form.entries());
    try {
      const response = await fetch("/api/early-access", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ...body, audience, consent: form.get("consent") === "on" }),
      });
      const result = await response.json().catch(() => ({})) as { error?: string };
      if (!response.ok) throw new Error(result.error || "We could not save your request. Please try again.");
      setState("success");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "We could not save your request. Please try again.");
      setState("error");
    }
  }

  if (state === "success") return <div className="form-success" role="status"><span>REQUEST RECEIVED</span><h2>You’re on the list.</h2><p>We saved your cloud access request and will use the deployment context you shared to plan the right preview track.</p><Link className="text-link" href="/research">Review the research record <span>↗</span></Link></div>;

  const enterprise = audience === "enterprise";
  const team = audience !== "personal";
  return <form className="early-form" onSubmit={submit} noValidate>
    <fieldset className="audience-picker"><legend>What best describes you?</legend>{(["personal", "team", "enterprise"] as Audience[]).map((value) => <label key={value}><input type="radio" name="audience-choice" checked={audience === value} onChange={() => setAudience(value)} /><span>{value === "personal" ? "Personal" : value === "team" ? "Team / startup" : "Enterprise"}</span></label>)}</fieldset>
    <label>Email<input name="email" type="email" autoComplete="email" placeholder="you@company.com" required /></label>
    <label>Name <span>optional</span><input name="name" type="text" autoComplete="name" placeholder="Your name" maxLength={120} /></label>
    {team && <label>Company <span>optional</span><input name="company" type="text" autoComplete="organization" placeholder="Company or team" maxLength={160} /></label>}
    {team && <label>Expected memory volume <span>optional</span><select name="volumeBand" defaultValue=""><option value="" disabled>Select a range</option><option value="under-100k">Under 100k records</option><option value="100k-1m">100k–1m records</option><option value="1m-10m">1m–10m records</option><option value="over-10m">Over 10m records</option></select></label>}
    <label className="wide">What are you building? <span>optional</span><textarea name="project" rows={4} maxLength={1500} placeholder="Agent workflow, memory problem, and what the system needs to remember." /></label>
    {enterprise && <label className="wide">Deployment requirements <span>optional</span><textarea name="deploymentRequirements" rows={3} maxLength={1500} placeholder="Cloud boundary, region, governance, retention, or security requirements." /></label>}
    <label className="honeypot" aria-hidden="true">Website<input name="website" type="text" tabIndex={-1} autoComplete="off" /></label>
    <label className="consent wide"><input name="consent" type="checkbox" required /><span>I agree that NarratorDB may store these details and contact me about cloud access. See the <a href="#privacy">privacy notice</a>.</span></label>
    {error && <p className="form-error wide" role="alert">{error}</p>}
    <button className="button primary wide" type="submit" disabled={state === "submitting"}>{state === "submitting" ? "Saving request…" : state === "error" ? "Retry request" : "Request cloud access"} <span>↗</span></button>
    <small className="wide">No account is created. We collect only the details entered here for preview planning.</small>
  </form>;
}
