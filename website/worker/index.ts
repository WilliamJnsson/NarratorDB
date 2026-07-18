/** Cloudflare Worker entry point for the vinext-starter template. */
import { handleImageOptimization, DEFAULT_DEVICE_SIZES, DEFAULT_IMAGE_SIZES } from "vinext/server/image-optimization";
import handler from "vinext/server/app-router-entry";

interface Env {
  ASSETS: Fetcher;
  DB: D1Database;
  IMAGES: {
    input(stream: ReadableStream): {
      transform(options: Record<string, unknown>): {
        output(options: { format: string; quality: number }): Promise<{ response(): Response }>;
      };
    };
  };
}

interface ExecutionContext {
  waitUntil(promise: Promise<unknown>): void;
  passThroughOnException(): void;
}

const earlyAccessAudiences = new Set(["personal", "team", "enterprise"]);
const earlyAccessVolumeBands = new Set(["", "under-100k", "100k-1m", "1m-10m", "over-10m"]);

function cleanLeadValue(value: unknown, limit: number) {
  return typeof value === "string" ? value.trim().slice(0, limit) : "";
}

function invalidLead(error: string) {
  return Response.json({ ok: false, error }, { status: 400 });
}

async function saveEarlyAccessLead(request: Request, env: Env) {
  let input: Record<string, unknown>;
  try {
    input = await request.json() as Record<string, unknown>;
  } catch {
    return invalidLead("Submit the form again. The request could not be read.");
  }
  if (cleanLeadValue(input.website, 200)) return Response.json({ ok: true });

  const email = cleanLeadValue(input.email, 254).toLowerCase();
  const audience = cleanLeadValue(input.audience, 24);
  const volumeBand = cleanLeadValue(input.volumeBand, 24);
  if (!/^\S+@\S+\.\S+$/.test(email)) return invalidLead("Enter a valid email address.");
  if (!earlyAccessAudiences.has(audience)) return invalidLead("Choose a personal, team, or enterprise track.");
  if (input.consent !== true) return invalidLead("Consent is required to save your request.");
  if (!earlyAccessVolumeBands.has(volumeBand)) return invalidLead("Choose a valid memory-volume range.");

  const now = Date.now();
  try {
    if (!env.DB) throw new Error("D1 binding unavailable");
    await env.DB.prepare(`
      INSERT INTO early_access_leads
        (id, email, audience, name, company, project, volume_band, deployment_requirements, consent_version, consent_at, status, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
      ON CONFLICT(email) DO UPDATE SET
        audience = excluded.audience,
        name = excluded.name,
        company = excluded.company,
        project = excluded.project,
        volume_band = excluded.volume_band,
        deployment_requirements = excluded.deployment_requirements,
        consent_version = excluded.consent_version,
        consent_at = excluded.consent_at,
        updated_at = excluded.updated_at
    `).bind(
      crypto.randomUUID(), email, audience,
      cleanLeadValue(input.name, 120) || null,
      cleanLeadValue(input.company, 160) || null,
      cleanLeadValue(input.project, 1500) || null,
      volumeBand || null,
      cleanLeadValue(input.deploymentRequirements, 1500) || null,
      "cloud-preview-2026-07-15", now, now, now,
    ).run();
    return Response.json({ ok: true });
  } catch (error) {
    console.error("early-access persistence failed", error instanceof Error ? error.message : "unknown error");
    return Response.json({ ok: false, error: "We could not save your request. Please try again." }, { status: 503 });
  }
}

// Image security config. SVG sources with .svg extension auto-skip the
// optimization endpoint on the client side (served directly, no proxy).
// To route SVGs through the optimizer (with security headers), set
// dangerouslyAllowSVG: true in next.config.js and uncomment below:
// const imageConfig: ImageConfig = { dangerouslyAllowSVG: true };

const worker = {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/api/early-access" && request.method === "POST") {
      return saveEarlyAccessLead(request, env);
    }

    if (url.pathname === "/_vinext/image") {
      const allowedWidths = [...DEFAULT_DEVICE_SIZES, ...DEFAULT_IMAGE_SIZES];
      return handleImageOptimization(request, {
        fetchAsset: (path) => env.ASSETS.fetch(new Request(new URL(path, request.url))),
        transformImage: async (body, { width, format, quality }) => {
          const result = await env.IMAGES.input(body).transform(width > 0 ? { width } : {}).output({ format, quality });
          return result.response();
        },
      }, allowedWidths);
    }

    return handler.fetch(request, env, ctx);
  },
};

export default worker;
