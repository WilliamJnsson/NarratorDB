CREATE TABLE `early_access_leads` (
	`id` text PRIMARY KEY NOT NULL,
	`email` text NOT NULL,
	`audience` text NOT NULL,
	`name` text,
	`company` text,
	`project` text,
	`volume_band` text,
	`deployment_requirements` text,
	`consent_version` text NOT NULL,
	`consent_at` integer NOT NULL,
	`status` text DEFAULT 'new' NOT NULL,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `early_access_leads_email_unique` ON `early_access_leads` (`email`);