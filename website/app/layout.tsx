import type { Metadata } from "next";
import { IBM_Plex_Mono, Inter } from "next/font/google";
import { headers } from "next/headers";
import "./globals.css";

const inter = Inter({ variable: "--font-inter", subsets: ["latin"] });
const plexMono = IBM_Plex_Mono({ variable: "--font-plex-mono", subsets: ["latin"], weight: ["400", "500"] });

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const host = requestHeaders.get("x-forwarded-host") ?? requestHeaders.get("host") ?? "narratordb.dev";
  const protocol = requestHeaders.get("x-forwarded-proto") ?? (host.includes("localhost") ? "http" : "https");
  const origin = `${protocol}://${host}`;
  return {
    title: "NarratorDB — Cloud memory infrastructure for AI",
    description:
      "Proprietary cloud memory with canonical records, transparent retrieval, explicit provenance, and reproducible research.",
    metadataBase: new URL(origin),
    icons: { icon: "/icon.svg" },
    alternates: { types: { "text/plain": "/llms.txt" } },
    openGraph: {
      title: "NarratorDB — Memory infrastructure that keeps the source",
      description: "Cloud memory, transparent retrieval, and research with the methodology attached.",
      type: "website",
    },
    twitter: {
      card: "summary",
      title: "NarratorDB — Memory infrastructure that keeps the source",
      description: "Cloud memory, transparent retrieval, and research with the methodology attached.",
    },
  };
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className={`${inter.variable} ${plexMono.variable}`}>{children}</body>
    </html>
  );
}
