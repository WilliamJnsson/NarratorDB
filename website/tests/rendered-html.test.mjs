import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const canonicalRoutes = ["/", "/product", "/research", "/pricing", "/early-access"];
const dashboardRoutes = ["/dashboard", "/dashboard/records", "/dashboard/entities", "/dashboard/retrieval", "/dashboard/activity", "/dashboard/integrations", "/dashboard/keys", "/dashboard/team", "/dashboard/usage", "/dashboard/settings"];
const redirects = {
  "/integrations": "/product#interfaces",
  "/enterprise": "/product#deployment",
  "/solutions": "/product#use-cases",
  "/solutions/customer-support": "/product#use-cases",
  "/resources": "/research#providers",
  "/benchmarks": "/research#methodology",
  "/about": "/product#principles",
  "/contact": "/early-access",
};

const assetBinding = { fetch: async () => new Response("Not found", { status: 404 }) };

async function request(path = "/", init = {}, bindings = {}) {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}-${Math.random()}`);
  const { default: worker } = await import(workerUrl.href);
  const headers = new Headers(init.headers);
  if (!headers.has("accept")) headers.set("accept", "text/html");
  return worker.fetch(new Request(`http://localhost${path}`, { ...init, headers, redirect: "manual" }), { ASSETS: assetBinding, ...bindings }, { waitUntil() {}, passThroughOnException() {} });
}

async function render(path = "/") {
  return request(path);
}

test("renders the five canonical NarratorDB pages", async () => {
  for (const route of canonicalRoutes) {
    const response = await render(route);
    assert.equal(response.status, 200, `${route} should render`);
    assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);
    const html = await response.text();
    assert.match(html, /NarratorDB/);
    assert.match(html, /cloud/i);
    assert.doesNotMatch(html, /#f2f0e9|Your site is taking shape|react-loading-skeleton/);
  }
});

test("renders the complete functional dashboard preview", async () => {
  for (const route of dashboardRoutes) {
    const response = await render(route);
    assert.equal(response.status, 200, `${route} should render`);
    const html = await response.text();
    assert.match(html, /NarratorDB/);
    assert.match(html, /Functional preview/i);
  }

  const overview = await (await render("/dashboard")).text();
  assert.match(overview, /Memory operations, without the black box/i);
  assert.match(overview, /Canonical records/);
  assert.match(overview, /Evidence attached/);
  assert.match(overview, /Reset preview/);

  const retrieval = await (await render("/dashboard/retrieval")).text();
  assert.match(retrieval, /Retrieval lab/);
  assert.match(retrieval, /RANKED EVIDENCE/);
  assert.match(retrieval, /CONTEXT BLOCK/);

  const keys = await (await render("/dashboard/keys")).text();
  assert.match(keys, /API keys/);
  assert.match(keys, /Project scoped/);
  assert.doesNotMatch(keys, /ndb_preview_A7p9K2m4X6q8/);
});

test("keeps visible interface type legible and reveal content readable by default", async () => {
  const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  const fontDeclarations = [...css.matchAll(/\bfont(?:-size)?\s*:\s*([^;{}]+)/g)].map((match) => match[1]);
  const pixelSizes = fontDeclarations.flatMap((value) => [...value.matchAll(/(\d+(?:\.\d+)?)px\b/g)].map((match) => Number(match[1])));
  const labelToken = css.match(/--type-label:\s*(\d+(?:\.\d+)?)px/);
  const hiddenReveal = css.match(/\.reveal-ready:not\(\.is-visible\)\s*\{([^}]*)\}/);

  assert.ok(pixelSizes.length > 0, "expected explicit CSS type sizes");
  assert.ok(pixelSizes.every((size) => size >= 11), `visible type must be at least 11px; found ${Math.min(...pixelSizes)}px`);
  assert.ok(labelToken && Number(labelToken[1]) >= 11, "the shared label token must remain at least 11px");
  assert.ok(hiddenReveal, "expected a pre-reveal style");
  assert.doesNotMatch(hiddenReveal[1], /opacity\s*:\s*0\b/, "reveal setup must not hide readable content");
});

test("keeps responsive homepage utilities accessible and mobile records readable", async () => {
  const aiDock = await readFile(new URL("../app/summarize-with-ai.tsx", import.meta.url), "utf8");
  const showcase = await readFile(new URL("../app/dashboard-showcase.tsx", import.meta.url), "utf8");
  const layout = await readFile(new URL("../app/layout.tsx", import.meta.url), "utf8");
  const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");

  assert.match(aiDock, /aria-expanded/);
  assert.match(aiDock, /aria-controls="ai-shortcuts-panel"/);
  assert.match(aiDock, /Collapse AI shortcuts/);
  assert.match(showcase, /landing-record-field/);
  assert.match(showcase, /aria-label={`Inspect \$\{record\.title\}`}/);
  assert.match(css, /\.landing-record-header\s*\{\s*display:\s*none/);
  assert.match(css, /\.ai-pop\.is-collapsed/);
  assert.match(layout, /icons:\s*\{\s*icon:\s*"\/icon\.svg"/);
});

test("renders the automatic multi-scenario memory showcase without playback controls", async () => {
  const home = await (await render("/")).text();
  for (const label of ["Personal assistant", "Care coordination", "Customer support", "Ingest", "Build", "Retrieve"]) {
    assert.match(home, new RegExp(label, "i"));
  }
  assert.match(home, /LIVE SIMULATION/);
  assert.match(home, /canonical text retained/i);
  assert.doesNotMatch(home, />\s*(?:Play|Pause|Replay)\s*</i);
});

test("shows the dashboard product on the landing page without publishing its review route in navigation", async () => {
  const home = await (await render("/")).text();
  for (const label of ["Operate memory like infrastructure", "Production memory at a glance", "Records", "Retrieval lab", "LIVE PRODUCT PREVIEW"]) {
    assert.match(home, new RegExp(label, "i"));
  }
  const showcase = await readFile(new URL("../app/dashboard-showcase.tsx", import.meta.url), "utf8");
  assert.match(showcase, /Every record keeps its source/i);
  assert.match(showcase, /Test retrieval before shipping it/i);
  assert.match(home, /Request dashboard access/i);
  assert.doesNotMatch(home, /<nav class="desktop-nav"[^>]*>[\s\S]*?Dashboard preview/i);
  assert.doesNotMatch(home, /<footer[\s\S]*?href="\/dashboard"/i);
});

test("renders the contained 3D hero memory field instead of the stripe treatment", async () => {
  const home = await (await render("/")).text();
  assert.match(home, /hero-memory-field/);
  assert.doesNotMatch(home, /hero-smoke/);
});

test("redirects removed pages to their canonical replacements", async () => {
  for (const [route, destination] of Object.entries(redirects)) {
    const response = await render(route);
    assert.ok([307, 308].includes(response.status), `${route} should redirect`);
    const location = new URL(response.headers.get("location"));
    assert.equal(`${location.pathname}${location.hash}`, destination);
  }
});

test("publishes sourced provider research and explicit comparability limits", async () => {
  const research = await (await render("/research")).text();
  for (const provider of ["NarratorDB", "Mem0", "Zep", "HydraDB", "Hindsight", "Supermemory", "Exabase", "Mastra", "LangMem", "Letta"]) {
    assert.match(research, new RegExp(provider));
  }
  assert.match(research, /82\.8%/);
  assert.match(research, /97\.4%/);
  assert.match(research, /not a controlled leaderboard/i);
  assert.match(research, /Source verified 2026-07-15/);
  assert.match(research, /EVIDENCE GAPS/);
  assert.match(research, /Primary source/);

  const record = JSON.parse(await readFile(new URL("../public/research/narratordb-longmemeval-2026-07-15.json", import.meta.url), "utf8"));
  assert.equal(record.status, "final-frozen");
  assert.equal(record.scores.top_50_accuracy_percent, 82.8);
  assert.equal(record.comparability.controlled_vendor_head_to_head, false);
});

test("keeps commercial positioning and preview status honest", async () => {
  const pages = await Promise.all(canonicalRoutes.map((route) => render(route).then((response) => response.text())));
  const [home, product, research, pricing, early] = pages;
  const commercial = pages.join("\n");
  assert.match(home, /private cloud/i);
  assert.match(product, /WAITLIST OPEN/);
  assert.match(product, /PRIVATE PREVIEW/);
  assert.match(product, /PLANNED/);
  for (const plan of ["Free", "Builder", "Pro", "Enterprise"]) assert.match(pricing, new RegExp(`>${plan}<`));
  assert.match(pricing, /preview targets/i);
  assert.match(early, /consent/i);
  assert.match(research, /historical benchmark record predates the cloud transition/i);
  assert.doesNotMatch(commercial, /View GitHub|MIT licensed|available under MIT|local engine available/i);
});

test("validates and persists adaptive early-access requests without fingerprint data", async () => {
  const saved = [];
  const DB = {
    prepare(sql) {
      assert.match(sql, /INSERT INTO early_access_leads/);
      assert.doesNotMatch(sql, /ip_address|user_agent/i);
      return { bind(...values) { return { async run() { saved.push(values); return { success: true }; } }; } };
    },
  };

  const valid = { email: " Founder@Example.com ", audience: "team", name: "Ari", company: "Example", project: "A production agent", volumeBand: "100k-1m", consent: true };
  const response = await request("/api/early-access", { method: "POST", headers: { "content-type": "application/json", accept: "application/json" }, body: JSON.stringify(valid) }, { DB });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true });
  assert.equal(saved.length, 1);
  assert.equal(saved[0][1], "founder@example.com");

  const invalidEmail = await request("/api/early-access", { method: "POST", headers: { "content-type": "application/json", accept: "application/json" }, body: JSON.stringify({ ...valid, email: "nope" }) }, { DB });
  assert.equal(invalidEmail.status, 400);
  const missingConsent = await request("/api/early-access", { method: "POST", headers: { "content-type": "application/json", accept: "application/json" }, body: JSON.stringify({ ...valid, consent: false }) }, { DB });
  assert.equal(missingConsent.status, 400);

  const honeypot = await request("/api/early-access", { method: "POST", headers: { "content-type": "application/json", accept: "application/json" }, body: JSON.stringify({ website: "bot.example", email: "bot@example.com" }) }, { DB });
  assert.equal(honeypot.status, 200);
  assert.equal(saved.length, 1);
});

test("returns not found for unknown routes", async () => {
  const response = await render("/not-a-real-page");
  assert.equal(response.status, 404);
});
