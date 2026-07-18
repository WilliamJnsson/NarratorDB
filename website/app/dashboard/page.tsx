import { DashboardApp } from "./dashboard-client";

export const metadata = {
  title: "Dashboard preview — NarratorDB",
  description: "A functional preview of the NarratorDB cloud control plane.",
};

export default function DashboardPage() {
  return <DashboardApp initialView="overview" />;
}
