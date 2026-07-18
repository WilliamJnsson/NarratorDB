export type BenchmarkKey = "longmemeval" | "locomo" | "beam-1m" | "beam-10m";

export type SourceRecord = {
  label: string;
  url: string;
  verified: string;
};

export type CategoryScores = {
  user: number;
  assistant: number;
  preference: number;
  update: number;
  temporal: number;
  multi: number;
};

export type BenchmarkResult = {
  providerId: string;
  benchmark: BenchmarkKey;
  score: number;
  metric: string;
  configuration: string;
  source: SourceRecord;
  categories?: CategoryScores;
  latencyP50?: number;
  contextTokens?: number;
  infrastructure?: "local" | "managed" | "mixed";
};

export type ProviderProfile = {
  id: string;
  name: string;
  kind: string;
  summary: string;
  architecture: string;
  retrieval: string;
  temporal: string;
  deployment: string;
  license: string;
  strengths: string[];
  publicGaps: string[];
  source: SourceRecord;
};

const verified = "2026-07-15";

export const sources = {
  narrator: { label: "NarratorDB sanitized frozen record", url: "/research/narratordb-longmemeval-2026-07-15.json", verified },
  mem0: { label: "Mem0 memory evaluation", url: "https://docs.mem0.ai/core-concepts/memory-evaluation", verified },
  zep: { label: "Zep research", url: "https://www.getzep.com/research/", verified },
  hydra: { label: "HydraDB LongMemEval report", url: "https://research.hydradb.com/hydradb.pdf", verified },
  hydraBeam: { label: "HydraDB BEAM 1M report", url: "https://benchmarks.hydradb.com/beam.pdf", verified },
  hindsight: { label: "Hindsight benchmarks", url: "https://benchmarks.hindsight.vectorize.io/", verified },
  supermemory: { label: "Supermemory LongMemEval research", url: "https://supermemory.ai/research/longmembench/", verified },
  exabase: { label: "Exabase M-1 research", url: "https://exabase.io/research/exabase-achieves-state-of-the-art-on-longmemeval-benchmark", verified },
  mastra: { label: "Mastra Observational Memory research", url: "https://mastra.ai/research/observational-memory", verified },
  langmem: { label: "LangMem documentation", url: "https://langchain-ai.github.io/langmem/", verified },
  letta: { label: "Letta memory benchmark research", url: "https://www.letta.com/blog/benchmarking-ai-agent-memory/", verified },
} satisfies Record<string, SourceRecord>;

export const benchmarkResults: BenchmarkResult[] = [
  {
    providerId: "narratordb", benchmark: "longmemeval", score: 82.8,
    metric: "answer accuracy · top 50", configuration: "GLM 5.2 answerer · DeepSeek V4 Flash judge · all 500 questions",
    source: sources.narrator, latencyP50: 19.05, infrastructure: "local",
    categories: { user: 97.1, assistant: 96.4, preference: 83.3, update: 91, temporal: 82, multi: 65.4 },
  },
  {
    providerId: "mem0", benchmark: "longmemeval", score: 94.4,
    metric: "answer accuracy · top 200", configuration: "Managed platform · production-representative model stack · ±1 point stated interval",
    source: sources.mem0, latencyP50: 2468.1, infrastructure: "managed", contextTokens: 6787,
    categories: { user: 98.6, assistant: 98.2, preference: 96.7, update: 93.6, temporal: 97, multi: 88 },
  },
  {
    providerId: "zep", benchmark: "longmemeval", score: 90.2,
    metric: "answer accuracy", configuration: "GPT-5.4 reader and judge · multi-scope retrieval",
    source: sources.zep, latencyP50: 104, infrastructure: "managed", contextTokens: 4408,
    categories: { user: 94.3, assistant: 96.4, preference: 90, update: 93.6, temporal: 90.2, multi: 83.5 },
  },
  {
    providerId: "hydradb", benchmark: "longmemeval", score: 90.79,
    metric: "answer accuracy", configuration: "Gemini 3 Pro evaluation · session-based ingestion",
    source: sources.hydra, infrastructure: "managed",
    categories: { user: 100, assistant: 100, preference: 96.67, update: 97.4, temporal: 90.97, multi: 76.69 },
  },
  {
    providerId: "hindsight", benchmark: "longmemeval", score: 94.6,
    metric: "AMB score", configuration: "Agent Memory Benchmark published result",
    source: sources.hindsight, infrastructure: "mixed",
  },
  {
    providerId: "supermemory", benchmark: "longmemeval", score: 95,
    metric: "LLM-as-judge · Recall@15 with aggregation", configuration: "GPT-4o · session-based ingestion · ~720 mean tokens",
    source: sources.supermemory, infrastructure: "managed", contextTokens: 720,
    categories: { user: 97, assistant: 100, preference: 90, update: 99, temporal: 91, multi: 93 },
  },
  {
    providerId: "exabase", benchmark: "longmemeval", score: 96.4,
    metric: "answer accuracy · top 50", configuration: "Gemini 3 Flash · multi-query retrieval",
    source: sources.exabase, infrastructure: "managed",
    categories: { user: 98.6, assistant: 100, preference: 96.7, update: 97.4, temporal: 95.5, multi: 94 },
  },
  {
    providerId: "mastra", benchmark: "longmemeval", score: 94.87,
    metric: "answer accuracy", configuration: "Observational Memory · GPT-5 mini actor · Gemini 2.5 Flash ingestion",
    source: sources.mastra, infrastructure: "mixed",
  },
  { providerId: "mem0", benchmark: "locomo", score: 92.5, metric: "answer accuracy", configuration: "Managed platform · 6,956 mean tokens", source: sources.mem0, contextTokens: 6956, infrastructure: "managed" },
  { providerId: "zep", benchmark: "locomo", score: 94.7, metric: "answer accuracy", configuration: "GPT-5.4 reader and judge · 5,760 median tokens", source: sources.zep, contextTokens: 5760, latencyP50: 87, infrastructure: "managed" },
  { providerId: "hindsight", benchmark: "locomo", score: 92, metric: "AMB score", configuration: "LoCoMo 10 published result", source: sources.hindsight, infrastructure: "mixed" },
  { providerId: "letta", benchmark: "locomo", score: 74, metric: "answer accuracy", configuration: "Filesystem memory · GPT-4o mini", source: sources.letta, infrastructure: "local" },
  { providerId: "mem0", benchmark: "beam-1m", score: 64.1, metric: "BEAM score", configuration: "Managed platform · 6,719 mean tokens", source: sources.mem0, contextTokens: 6719, infrastructure: "managed" },
  { providerId: "hydradb", benchmark: "beam-1m", score: 82, metric: "average across ten dimensions", configuration: "GPT-5.4 judge · Hindsight-compatible evaluation configuration", source: sources.hydraBeam, infrastructure: "managed" },
  { providerId: "hindsight", benchmark: "beam-1m", score: 73.9, metric: "AMB score", configuration: "1M-token tier", source: sources.hindsight, infrastructure: "mixed" },
  { providerId: "mem0", benchmark: "beam-10m", score: 48.6, metric: "BEAM score", configuration: "Managed platform · 6,914 mean tokens", source: sources.mem0, contextTokens: 6914, infrastructure: "managed" },
  { providerId: "hindsight", benchmark: "beam-10m", score: 64.1, metric: "AMB score", configuration: "10M-token tier", source: sources.hindsight, infrastructure: "mixed" },
];

export const providerProfiles: ProviderProfile[] = [
  {
    id: "narratordb", name: "NarratorDB", kind: "Proprietary cloud memory platform",
    summary: "A source-first cloud memory layer built around canonical records, bounded retrieval, provenance, and an inspectable operating boundary.",
    architecture: "Managed canonical storage, lexical and semantic indexes, provenance, typed artifacts, relations, and contextual windows.",
    retrieval: "Multi-signal retrieval with query-aware reranking and adjacent evidence composition; the cited score comes from the frozen historical 1.3 engine.",
    temporal: "Preserves original timestamps and source text; cross-session evidence composition remains the primary research target.",
    deployment: "Commercial managed cloud in private-preview planning; private-boundary options remain planned.",
    license: "Proprietary cloud product; the cited historical benchmark record predates the cloud transition.",
    strengths: ["Canonical source records", "Inspectable evidence path", "19.05 ms historical local median", "Sanitized first-party benchmark record"],
    publicGaps: ["No controlled same-model vendor run", "Multi-session answer synthesis at 65.4%", "No published BEAM score"], source: sources.narrator,
  },
  {
    id: "mem0", name: "Mem0", kind: "Managed + open-source memory",
    summary: "A production memory layer combining ADD-only extraction, vector search, entity links, temporal metadata, and ranked multi-signal retrieval.",
    architecture: "Vector database, entity graph, and SQL history layers connected by asynchronous extraction and temporal enrichment.",
    retrieval: "Semantic, BM25, entity, and temporal signals fused into a top-k result set.",
    temporal: "Stores old and new facts together and scores extracted temporal metadata at recall time.",
    deployment: "Managed platform plus a self-hosted open-source backend with different expected results.",
    license: "Open-source SDK/backend plus proprietary managed-platform optimizations.",
    strengths: ["Broad benchmark suite", "Open evaluation harness", "Managed and OSS paths", "Token reporting"],
    publicGaps: ["Managed results do not represent OSS exactly", "Current page omits per-query latency", "Top-200 result differs from NarratorDB top-50"], source: sources.mem0,
  },
  {
    id: "zep", name: "Zep", kind: "Temporal context graph",
    summary: "A graph-based memory and context lake designed to compose facts, entities, episodes, observations, and summaries.",
    architecture: "Temporal context graph with in-memory graph structures, vector/BM25 indexes, observations, and thread summaries.",
    retrieval: "Parallel multi-scope search or a single auto-search endpoint with cross-scope reranking.",
    temporal: "Temporal invalidation and graph history keep facts current across sessions.",
    deployment: "Commercial managed service with Graphiti available as an open-source graph framework.",
    license: "Commercial Zep service; Graphiti is separately open source.",
    strengths: ["Published p50/p95 latency", "Published context size", "Strong multi-session score", "Detailed methodology"],
    publicGaps: ["GPT-5.4 setup differs from other reports", "Multi-scope result requires client composition", "No BEAM result on current research page"], source: sources.zep,
  },
  {
    id: "hydradb", name: "HydraDB", kind: "Versioned graph database",
    summary: "A graph-first context engine that models knowledge as versioned, relational, time-aware state.",
    architecture: "Sliding-window enrichment, Git-style versioned knowledge graph, and multi-stage retrieval.",
    retrieval: "Dense and sparse search, graph traversal, query expansion, and reranking.",
    temporal: "Append-only versioned edges preserve what changed, when, and why.",
    deployment: "Managed service positioned for graph storage, agent memory, and enterprise context infrastructure.",
    license: "Proprietary service and research implementation details; not presented as an OSS database.",
    strengths: ["Strong temporal categories", "LongMemEval and BEAM reports", "Versioned relationships", "Graph-native positioning"],
    publicGaps: ["Vendor-authored reports", "Limited public latency detail", "Different evaluators across benchmark reports"], source: sources.hydra,
  },
  {
    id: "hindsight", name: "Hindsight", kind: "Biomimetic memory",
    summary: "An open memory system that extracts facts, resolves entities, and consolidates higher-order observations.",
    architecture: "Retain, recall, and reflect operations over facts, entities, and consolidated mental models.",
    retrieval: "Embedding retrieval plus reranking and reflective synthesis.",
    temporal: "Tracks evolving facts and exposes freshness-aware consolidation behavior.",
    deployment: "Local daemon and cloud service share the same API.",
    license: "MIT-licensed local implementation with an optional commercial cloud.",
    strengths: ["Public AMB harness", "Local and cloud", "Strong 10M BEAM result", "Broad integration ecosystem"],
    publicGaps: ["Headline page compresses model details", "Different benchmark harness from NarratorDB", "Latency not shown beside headline scores"], source: sources.hindsight,
  },
  {
    id: "supermemory", name: "Supermemory", kind: "Managed memory API",
    summary: "A memory engine combining atomic memories, original chunks, relational versioning, temporal grounding, and hybrid search.",
    architecture: "Contextual memory extraction, update/extend/derive relations, dual timestamps, and session ingestion.",
    retrieval: "Semantic retrieval over atomic memories, followed by injection of the original source chunk.",
    temporal: "Separates document date from event date and maintains relationship-based knowledge evolution.",
    deployment: "Managed API with public benchmark tooling and documentation.",
    license: "Commercial service; benchmark runner and selected tooling are public.",
    strengths: ["~720-token reported context", "Original chunk return", "Strong multi-session score", "Reproduction materials"],
    publicGaps: ["Recall@15 aggregation differs from other cutoffs", "Session ingestion differs from original protocol", "No directly published latency in the cited report"], source: sources.supermemory,
  },
  {
    id: "exabase", name: "Exabase M-1", kind: "Proprietary memory engine",
    summary: "A reconstructive retrieval engine using semantic, lexical, temporal, importance, and coherence signals.",
    architecture: "Multi-query decomposition, temporal chains, cross-memory coherence, and proprietary reranking.",
    retrieval: "Parallel subqueries assemble complementary evidence before a final ranking stage.",
    temporal: "Temporal salience and anchor-event resolution influence retrieval and ordering.",
    deployment: "Commercial data layer; detailed implementation and learned parameters remain proprietary.",
    license: "Proprietary.",
    strengths: ["96.4% top-50 report", "Gemini 3 Flash reader", "Published cutoff curve", "Strong multi-session category"],
    publicGaps: ["No public production latency", "Limited disclosed implementation", "New vendor-authored result"], source: sources.exabase,
  },
  {
    id: "mastra", name: "Mastra OM", kind: "Observational memory",
    summary: "A stable-context memory approach where Observer and Reflector agents replace older raw messages with a dense event log.",
    architecture: "Background observation and reflection maintain a bounded, prompt-cacheable text memory.",
    retrieval: "No dynamic per-query retrieval; the actor receives the maintained observation log.",
    temporal: "Dated observations capture events, decisions, and changes over time.",
    deployment: "Open-source implementation inside the Mastra agent framework.",
    license: "Open source.",
    strengths: ["Stable context window", "Prompt-cacheable", "Open implementation", "Multiple reader-model reports"],
    publicGaps: ["Ingestion requires background model work", "Not a standalone database", "Reader choice materially changes score"], source: sources.mastra,
  },
  {
    id: "langmem", name: "LangMem", kind: "Memory toolkit",
    summary: "LangChain primitives for extracting memories, managing them in hot or background paths, and optimizing agent behavior.",
    architecture: "LLM-driven memory managers layered on LangGraph long-term stores or application-selected storage.",
    retrieval: "Depends on the configured LangGraph store and application-specific memory design.",
    temporal: "Application-defined; memories may be consolidated or updated through LLM-managed workflows.",
    deployment: "Python library with native LangGraph Platform integration.",
    license: "Open-source library; LangGraph Platform is commercial.",
    strengths: ["Framework integration", "Hot- and background-path APIs", "Storage flexibility", "Prompt optimization"],
    publicGaps: ["No current first-party comparable LongMemEval headline", "Performance depends on selected store", "LLM calls are central to memory formation"], source: sources.langmem,
  },
  {
    id: "letta", name: "Letta", kind: "Stateful agent framework",
    summary: "An agent runtime descended from MemGPT, emphasizing agents that actively manage their own memory and context.",
    architecture: "Agent-managed memory blocks, archival storage, tools, and filesystem-oriented context strategies.",
    retrieval: "The agent decides how and when to use memory tools rather than relying on a single fixed retrieval pipeline.",
    temporal: "Depends on agent behavior and stored records rather than a dedicated temporal database layer.",
    deployment: "Open-source server and managed platform options.",
    license: "Open-source core with commercial services.",
    strengths: ["Agentic memory model", "74% filesystem LoCoMo study", "Stateful runtime", "Open research"],
    publicGaps: ["LoCoMo result is not LongMemEval", "Agent behavior is part of the measured system", "No comparable retrieval-latency headline"], source: sources.letta,
  },
];

export const providerName = (id: string) => providerProfiles.find((provider) => provider.id === id)?.name ?? id;

export function validateResearchData(): string[] {
  const problems: string[] = [];
  for (const result of benchmarkResults) {
    if (!result.source.url || !result.source.verified) problems.push(`${result.providerId}:${result.benchmark} missing source metadata`);
    if (!result.configuration || !result.metric) problems.push(`${result.providerId}:${result.benchmark} missing methodology metadata`);
    if (result.score < 0 || result.score > 100) problems.push(`${result.providerId}:${result.benchmark} has an invalid score`);
  }
  for (const provider of providerProfiles) {
    if (!provider.source.url || !provider.source.verified) problems.push(`${provider.id} missing profile source metadata`);
    if (!provider.publicGaps.length) problems.push(`${provider.id} missing public limitations`);
  }
  return problems;
}
