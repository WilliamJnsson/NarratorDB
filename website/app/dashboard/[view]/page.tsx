import { notFound } from "next/navigation";
import { DashboardApp } from "../dashboard-client";
import { dashboardViews, type DashboardView } from "../dashboard-config";

type PageProps = { params: Promise<{ view: string }> };

export async function generateMetadata({ params }: PageProps) {
  const { view } = await params;
  const label = dashboardViews.find((item) => item.id === view)?.label;
  return { title: label ? `${label} — NarratorDB dashboard` : "NarratorDB dashboard" };
}

export default async function DashboardViewPage({ params }: PageProps) {
  const { view } = await params;
  if (!dashboardViews.some((item) => item.id === view)) notFound();
  return <DashboardApp initialView={view as DashboardView} />;
}
