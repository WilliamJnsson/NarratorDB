# NarratorDB product website

The public-facing NarratorDB product website, built with React, vinext, and the
OpenAI Sites runtime. Its primary surfaces are the homepage, product, research,
pricing, early access, and a functional dashboard preview. Removed legacy
routes redirect to the relevant canonical section.

## Local development

Requires Node.js 22.13 or newer.

```bash
npm install
npm run dev
```

Open the exact local URL printed by the development server. It will use the
next available port when `3000` is occupied.

The current private deployment is
[narratordb-home.william615395.chatgpt.site](https://narratordb-home.william615395.chatgpt.site).

## Production build

```bash
npm run build
```

The marketing pages and dashboard use explicit App Router files under `app/`.
`app/[...slug]/page.tsx` handles only legacy redirects and unknown routes.
Shared components, motion, pricing, and form controls live alongside the pages;
the visual system is in `app/globals.css`, while sourced provider/benchmark
records are in `app/research-data.ts` and `public/research/`.

Dashboard routes live under `app/dashboard/`. The preview provides an overview,
canonical record explorer, entity scopes, retrieval lab, activity logs,
integrations, API keys, team access, usage, and project settings. Its mutations
are deliberately session-scoped browser simulations with a visible reset path;
they must not be presented as production account, billing, or cloud-control
operations.

Run the full site checks with:

```bash
npm test
npm run lint
```

Benchmark values must remain aligned with frozen records in the parent
repository. Vendor references must keep their comparability disclaimers.
Pricing and private-deployment capabilities not in preview must remain labeled
as preview targets or Planned.

## Waitlist persistence

The worker handles `POST /api/early-access` before the vinext router and writes
validated, explicitly consented requests to the `early_access_leads` D1 table.
The binding is `DB` in `.openai/hosting.json`; its Drizzle schema is in
`db/schema.ts`, with generated migrations in `drizzle/`.

After changing the schema, generate and review a migration:

```bash
npm run db:generate
```

The endpoint normalizes email addresses, upserts duplicate submissions, uses a
honeypot for low-cost bot suppression, and stores no IP address or user-agent.

## Visual and interaction contract

- Keep the page frame white and primary text black, then separate major content
  bands with the restrained avant-garde palette: acid chartreuse, editorial
  pink, safety coral, industrial silver, and one inverted ink field.
- Use full color fields and slim multicolor rails as structural separators, not
  decorative gradients inside every component. Repeated content within a field
  should stay typographic and border-led.
- Keep data-dense research visualizations on white or industrial-gray fields;
  saturated section colors must not compete with orange chart marks or compress
  the perceived reading space.
- Use orange for chart marks. Use green only for primary actions and states
  that are currently available, healthy, or ready; planned states stay neutral.
- Preserve the homepage hero's empty upper field and bottom-left copy. Its
  grayscale 3D memory field stays inside the border, concentrates depth in the
  upper-right space, responds only subtly to pointer position, suspends
  offscreen, and becomes a deterministic static composition under
  `prefers-reduced-motion`.
- Keep chart values in their dedicated column so high scores cannot overlap the
  plot edge. Recheck this at desktop and mobile widths after chart changes.
- Keep all visible interface type at 11 px or larger. Labels, axes, and status
  metadata use the shared label token; body copy should normally sit at 14–16
  px with relaxed line height, readable measure, and near-black contrast on
  every light or saturated field.
- Reveal motion may translate content into place, but it must never make copy
  fully transparent while JavaScript or an observer is still initializing.
- The homepage live showcase is a deterministic product simulation, not a live
  cloud request. It continuously cycles Personal Assistant, fictional Care
  Coordination, and Customer Support stories through Ingest, Build, and
  Retrieve. Scenario changes restart from Ingest; stage buttons seek forward or
  backward without exposing play, pause, or replay controls. Autoplay suspends
  offscreen and becomes a static completed view under reduced motion.
- Preserve source links, verification dates, evidence gaps, and non-identical
  methodology warnings whenever research data changes.
- Keep dashboard surfaces dense but breathable: white application panels,
  black hierarchy, orange data marks, green healthy/ready states, and saturated
  accent colors only where they clarify state. Tables and charts must remain
  contained at desktop and mobile widths, and every simulated mutation must
  remain visibly identifiable as preview behavior.

The automated suite covers the production build, ten route/data/API/style
checks, legacy redirects, commercial copy, research/pricing integrity, D1 lead
validation, unknown-route handling, the 11 px visible-type floor, and readable
pre-reveal content. It renders all ten dashboard views, verifies the preview
boundary and key-safety behavior, prevents manual playback controls from entering the
automatic showcase and prevents the retired hero stripe treatment from
returning. Visual changes should still be reviewed at roughly 1440 px, 1024
px, and 390 px, including chart controls, navigation, form states, overflow,
focus, zoom, and reduced-motion behavior.
