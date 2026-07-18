import { notFound, redirect } from "next/navigation";

type PageProps = { params: Promise<{ slug: string[] }> };

const legacyRoutes: Record<string, string> = {
  integrations: "/product#interfaces",
  enterprise: "/product#deployment",
  solutions: "/product#use-cases",
  "solutions/customer-support": "/product#use-cases",
  "solutions/sales-crm": "/product#use-cases",
  "solutions/healthcare": "/product#use-cases",
  "solutions/developer-agents": "/product#use-cases",
  about: "/product#principles",
  benchmarks: "/research#methodology",
  resources: "/research#providers",
  "resources/context-engineering": "/research#methodology",
  "resources/buy-vs-build": "/research#providers",
  contact: "/early-access",
};

export default async function LegacyPage({ params }: PageProps) {
  const key = (await params).slug.join("/");
  if (legacyRoutes[key]) redirect(legacyRoutes[key]);
  notFound();
}
