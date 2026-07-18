import type { Metadata } from "next";
import { HeroStat, MarketingShell, PageHero, PageSection } from "../components";
import { EarlyAccessForm } from "../interactions";

export const metadata: Metadata = {
  title: "Cloud access — NarratorDB",
  description: "Request access to the NarratorDB Cloud private preview.",
  alternates: { canonical: "/early-access" },
};

export default function EarlyAccessPage() {
  const hero = <PageHero eyebrow="Cloud access · waitlist open" title="Tell us where durable memory needs to run." lede="Request a preview track for a personal project, a production team, or an organization with deployment requirements." aside={<HeroStat label="REQUEST STATUS" value="Open" detail="Saved securely · reviewed by track" tone="ready" />} />;
  return <MarketingShell hero={hero}>
    <PageSection id="request" label="01 · REQUEST ACCESS" title="A little architecture is more useful than a generic lead form." body="Choose the track that fits. The form adapts so you can share only the project and deployment context relevant to your preview.">
      <div className="access-layout"><div className="access-intro"><div className="access-points"><span>Managed cloud projects and pricing</span><span>Research and controlled benchmark participation</span><span>Enterprise boundary and governance planning</span></div><p>Submitting does not create an account or guarantee access. We will use the information to sequence the private preview.</p></div><EarlyAccessForm /></div>
    </PageSection>
    <PageSection id="privacy" label="02 · PRIVACY" title="A small, explicit lead record." body="NarratorDB stores only the information you enter, your consent version, and submission timestamps for private-preview planning." tone="pink">
      <div className="privacy-copy"><p>We use the record to evaluate preview fit, understand deployment needs, and contact you about NarratorDB Cloud. We do not collect an IP address or browser fingerprint in this form.</p><p>You can request access, correction, or deletion of the record. A dedicated privacy contact will be published before the site opens beyond the private preview.</p></div>
    </PageSection>
  </MarketingShell>;
}
