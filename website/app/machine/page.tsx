import type { Metadata } from "next";
import { SITE_BRIEF, SITE_NAME } from "../ai-brief";

export const metadata: Metadata = {
  title: `${SITE_NAME} — Machine view`,
  description: `Plain-text machine-readable brief of ${SITE_NAME}, for AI agents and text-only clients.`,
};

export default function MachinePage() {
  return <main className="machine-page">
    <pre className="machine-brief">{SITE_BRIEF}</pre>
  </main>;
}
