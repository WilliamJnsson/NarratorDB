import { integer, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const earlyAccessLeads = sqliteTable("early_access_leads", {
  id: text("id").primaryKey(),
  email: text("email").notNull().unique(),
  audience: text("audience").notNull(),
  name: text("name"),
  company: text("company"),
  project: text("project"),
  volumeBand: text("volume_band"),
  deploymentRequirements: text("deployment_requirements"),
  consentVersion: text("consent_version").notNull(),
  consentAt: integer("consent_at").notNull(),
  status: text("status").notNull().default("new"),
  createdAt: integer("created_at").notNull(),
  updatedAt: integer("updated_at").notNull(),
});
