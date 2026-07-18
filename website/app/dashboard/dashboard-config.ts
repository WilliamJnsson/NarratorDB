export type DashboardView = "overview" | "records" | "entities" | "retrieval" | "activity" | "integrations" | "keys" | "team" | "usage" | "settings";

export const dashboardViews: { id: DashboardView; label: string; short: string; group: string }[] = [
  { id: "overview", label: "Overview", short: "OV", group: "Workspace" },
  { id: "records", label: "Canonical records", short: "CR", group: "Memory" },
  { id: "entities", label: "Entities & scopes", short: "ES", group: "Memory" },
  { id: "retrieval", label: "Retrieval lab", short: "RL", group: "Memory" },
  { id: "activity", label: "Activity", short: "AC", group: "Operate" },
  { id: "integrations", label: "Integrations", short: "IN", group: "Operate" },
  { id: "keys", label: "API keys", short: "AK", group: "Operate" },
  { id: "team", label: "Team & access", short: "TA", group: "Organization" },
  { id: "usage", label: "Usage", short: "US", group: "Organization" },
  { id: "settings", label: "Settings", short: "SE", group: "Organization" },
];
