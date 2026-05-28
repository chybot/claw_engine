# Distilled Patterns

Cross-source observations, organised per claw_engine layer. Each pattern ends with
**copy / adapt / avoid** for claw_engine specifically.

## Categorisation legend (introduced 2026-05-28 after L1 mis-attribution review)

Every OSS candidate falls into one of five categories. Mixing them up causes the
"LangGraph 借鉴 checkpointer" / "pydantic-settings 替合并" / "L1 编排"-class errors.

- 🟢 **Module replacement** — adapter swaps in OSS, engine Protocol unchanged
- 🔵 **Implementation aid** — OSS provides typing/utility, business logic stays
- 🟡 **Concept reference only** — read the docs, do **not** import
- ⚪ **No outsource** — intentionally thin, business-specific, or core abstraction
- 🔧 **CI / dev tooling** — not a module replacement, a process guard

---

## Layer-cutting pattern: "engine owns the contract, adapters own the SDK"

Every OSS framework that scales beyond a single use site exposes a thin **protocol/interface**
in its core and pushes integrations to satellite packages — Slack-bolt has `App` + middleware;
SQLAlchemy has `Dialect`; OpenTelemetry has `SpanProcessor` + `Exporter`; Casbin has `Adapter` +
`Watcher`. **claw_engine's 5-seam design is already aligned with this convention** — confirms the
direction. Implication: when adopting OSS, the OSS *implements* a seam, it does not replace one.

- **Copy**: "interface in core, plugin in satellite" pattern. ✅ Already done.
- **Adapt**: name your adapter packages so the OSS dep is obvious (`adapters/secrets/infisical/`,
  not `adapters/secrets/`).
- **Avoid**: importing an OSS framework's *opinionated orchestrator* (LangGraph's `StateGraph`,
  Letta's `Agent`) — they're not seam-fit, they're competing engines.

---

## L0 Channels — "verify-before-parse-before-route" is the universal shape

Every IM SDK reviewed (slack-bolt, botbuilder, telegram, standardwebhooks) splits inbound handling
into three near-identical phases that match your `MessagingGateway.verify_inbound / parse_inbound /
{route…}` split.

- **Copy**: keep the 3-phase split — it's load-bearing for security and is the industry shape.
- **Adapt (🟢 per-IM SDK)**: each `adapters/channels/<ch>/` defers signature compare to *that
  channel's* tested SDK — slack-bolt's `SignatureVerifier` for Slack, botbuilder for Teams,
  python-telegram-bot for TG, etc. This drops a class of timing-attack & header-case bugs.
- **Adapt (🔵 standardwebhooks)**: applies only when you *control both ends* or integrate with
  a Standard-Webhooks-conformant provider (Resend / Clerk / Vercel). It is **not** a generic
  signature library — Slack/Telegram/WeChat/SeaTalk each use proprietary formats.
- **Avoid**: full chatbot platforms (Chatwoot/Botpress) — they fuse verify+parse+route+respond
  into a black box, you lose the seam.

**Anti-pattern observed in algo-bot (`algo-bot-security-gaps`)**: webhook handler with a `TODO`
for token check. The OSS SDKs all *fail closed* by default; importing one mechanically removes
this whole bug class.

---

## L1 Session Dispatch — not an orchestrator, no OSS applies

> **Correction (2026-05-28)**: this section was originally titled "L1 Orchestration" and
> recommended "borrow LangGraph thread/checkpoint". Both were wrong.

claw L1 (`ConversationService`) does **not** orchestrate the agent loop. The agent loop runs
inside the L2 backend CLI (codex `--continue`, claude `--resume <session_id>`), which manages
message history, tool calls, summarisation itself. L1's job is three bookkeeping operations:

1. `(workspace_id, channel, external_thread_key)` ↔ `backend_thread_id` lookup
2. `message_id` dedup
3. `max_rounds` counter

LangGraph / Letta / Haystack are competing **orchestrators** — they want to own the loop. They
do not adapt as L1 implementations; they replace L2. Since claw deliberately delegated the loop
to the backend CLI, importing any of them at L1 would re-introduce the abstraction claw chose
to remove.

- **Copy** ⚪: nothing — L1 stays thin.
- **Borrow at L5 instead**: the `thread_id`-keyed schema idea only makes sense if you actually
  store turn history. claw doesn't. See L5 correction.
- **Avoid**: putting any "framework" at L1; treating dedup/rate-limit as a problem worth a lib.

---

## L2 Backend — universality is a benefit if proven

The contract suite that proves codex + claude interchangeability gets *more credible the more
backends pass it*. aider / OpenHands / gptme are all CLI-shaped — adding even one (aider) doubles
the empirical case for the abstraction.

- **Copy**: nothing (no OSS does this layering for code-agent CLIs).
- **Adapt**: aider as `adapters/backends/aider/`. Likely 1–2 day spike; immediately raises
  contract-suite weight from 5×2 → 5×3.
- **Avoid**: writing a backend against a SaaS-only API (chatgpt.com playwright) — fails the
  hermetic-test invariant.

---

## L3 Workflows — "durable" is the right keyword

The OSS landscape has bifurcated:

- **Task queues** (Celery / arq / dramatiq / RQ): no run history, no progress, no rerun-by-id.
  Your `WorkflowRun` model is richer than what they give you.
- **Durable execution** (Temporal / Hatchet / Inngest / DBOS): persistent run history, progress,
  rerun, retry policies. **This is the conceptual match for `WorkflowService` + `WorkflowRun`.**

Hatchet specifically calls itself a "drop-in replacement for Temporal or DBOS workflows" with a
typed Python SDK. **It is, today, the closest external implementation of the abstraction you
hand-rolled in P4.**

- **Copy**: nothing structural — your contract is already right.
- **Adapt**: Hatchet as `adapters/workflows/hatchet/` when in-memory `WorkflowStore` outgrows
  fit. Map `WorkflowRun.{status, progress, rerun_of}` ↔ Hatchet run; keep `WorkflowToolBridge`'s
  4 RBAC gates as the engine-side decorator (do **not** delegate RBAC to Hatchet's permissioning).
- **Avoid**: Celery — would be a step backward in concept fidelity.

---

## L4 Context — split config from secrets, and split *axis* of layering

Every mature framework separates:

- **Static config layering** (pydantic-settings / dynaconf / Hydra)
- **Secret retrieval** (Vault / Infisical / Doppler / AWS SM)

claw_engine already does this (`ConfigProvider` vs `SecretProvider`). Good.

**But — corrected 2026-05-28**: pydantic-settings layers configs **by source type** (env vars
vs .env vs secret file), not by claw's **organisational dimensions** (global ⊕ workspace ⊕ user
⊕ secret). So:

- 🔵 **pydantic-settings = schema + validation aid** for the merged result (`ResolvedWorkspace.env`).
  Not a replacement for the merge logic.
- ⚪ **LayeredConfigProvider's merge orchestration stays** — workspace×user is business semantics.

Licensing matters: HashiCorp's 2023 BSL re-license created **OpenBao** (MPL-2.0 fork) and pushed
mindshare to **Infisical** (MIT). Pure-MIT options are healthier for an OSS engine.

- **Copy**: Doppler / Infisical's "secret = named env var injected at process boundary" model
  (matches claw exactly).
- **Adapt (🟢)**: `InMemorySecretProvider` → Infisical adapter. redact logic stays engine-side.
- **Adapt (🔵)**: pydantic-settings v2 to *type-check* the merged env, not to *do* the merge.
- **Avoid**: persisting secrets via the same store as config (algo-bot smell: encrypted user
  API key in same SQLite table as settings, with the encryption key hard-coded — see
  `algo-bot-security-gaps`).

---

## L5 Persistence — ORM swap, no schema to borrow

> **Correction (2026-05-28)**: previously suggested borrowing LangGraph checkpointer schema.
> That was wrong — checkpointer assumes the framework owns the agent loop and stores full
> state blobs. claw's Session is a *one-row lookup* (handle to backend's own session). The
> abstractions don't match; there is nothing to borrow at the schema level.

The real lesson is single-step:

**Don't hand-write SQL across multiple dialects.** SQLAlchemy 2.x async covers sqlite + mysql +
postgres with identical code. Saves the "rewrite for mysql in P7" tax. That's the entire L5 OSS
play.

- **Copy**: idempotent `CREATE TABLE IF NOT EXISTS` migration on first connect (pattern is
  generic, not LangGraph-specific).
- **Adapt (🔵)**: SQLAlchemy async for `SqliteSessionStore` rewrite. Schema stays one-table;
  no `checkpoints/blobs` split because there is no turn history to store (it lives in the
  backend CLI's own session).
- **Avoid**:
  - ORM-everything bias — don't pull SQLAlchemy into `engine/persistence/` Protocol; keep
    contract pure dataclasses, SQLAlchemy in adapter.
  - LangGraph checkpointer as a "session schema reference" — wrong abstraction (see above).
  - Letta memory store — assumes long-term memory + summarisation is *your* problem.

---

## ⟂ Identity — separate `resolve_user` from `can_*`; each has different OSS fit

`IdentityProvider` does two unrelated things:

### resolve_user (identity)

- **Web/API channels with OIDC token** → 🟢 Keycloak / Authentik / Logto / Ory Kratos validate
  the token and return identity claims.
- **IM channels (Slack/Teams/Telegram/SeaTalk)** → ⚪ user is already authenticated by the IM
  itself. `resolve_user` is a mapping-table lookup `(channel, raw_user_ref) → User` against
  your own DB. **No OSS needed; an OIDC server here is over-engineering.**

### can_access_workspace / can_use_skill / can_run_workflow (authorisation)

Three architectural choices, pick by graph complexity:

| Pattern | Examples | Best fit |
|---|---|---|
| **Embedded library** | Casbin | Decisions are local-to-process, no central audit need |
| **Centralised PDP service** | Cerbos, OPA | Policies versioned independently of code |
| **Graph database (Zanzibar)** | OpenFGA, SpiceDB | Real relationship graph (org → team → user → resource) |

claw_engine V1 has 3 simple `can_*` calls with default-allow + denylist. **Embedded library
(Casbin)** matches exactly. Upgrade to Cerbos/OpenFGA only when:

- Policies need non-engineer review (→ Cerbos)
- User↔workspace↔skill becomes a real multi-hop graph (→ OpenFGA)

- **Copy**: Casbin's PERM (Policy/Effect/Request/Matcher) split — useful even if you don't import,
  forces you to name each axis.
- **Adapt (🟢)**: `InMemoryIdentityProvider` keeps user lookup; Casbin handles `can_*` decisions.
  Express default-allow as `e = !some(where (p.eft == deny))`.
- **Avoid**: `resolve_user` over-coupling — keep SSO (Keycloak/Authentik) and authz (Casbin)
  separate; a user identity service and a policy decision service are not the same thing,
  and most claw V1 deployments don't need the former at all.

---

## ⟂ Observability — OpenTelemetry is the universal layer

Every LLM-trace product (Langfuse, Phoenix, Helicone, LangSmith, Traceloop) **consumes OTLP**.
Langfuse specifically runs an OTLP HTTP endpoint and ships an OTel-native SDK v4. **There is no
LLM-tracing-specific contract anymore — there's OTel + LLM semantic conventions.**

Implication: your `Tracer` Protocol should produce OTel spans, not a custom format. Then
every backend (Langfuse, Phoenix, …) is free.

- **Copy**: OTel `Tracer` / `Span` API shape. Your Protocol can be a thin wrapper that produces
  real OTel spans when given a non-NoOp tracer.
- **Adapt**: `TraceDims` → span attributes with `claw.` prefix (or GenAI semantic conventions
  `gen_ai.*` where overlap exists).
- **Avoid**: building a Langfuse-specific adapter. Use the OTLP exporter.

**Invariant preservation**: redact-before-tracer stays engine-side. OTel does **not** redact for
you; it's a serialisation layer, not a policy layer.

---

## ⟂ Skills — no good OSS exists, keep self-built

Surveyed: copier, cookiecutter, git sparse-checkout, git submodule, terraform-style providers.
None solve "manifest-tracked provisioned-vs-not + revocation-on-next-sync + path-traversal-proof
sync of files into agent runtime directory". The closest analog is **dotbot / chezmoi** for
dotfile management — same "manifest of managed paths, idempotent sync" shape — but neither is
agent-skill-aware.

- **Copy**: dotbot's manifest format is worth reading as a sanity check of `.claw_provisioned.json`.
- **Adapt**: nothing — your implementation is already aligned with the best lineage available.
- **Avoid**: building skill provisioning on top of a templating tool (copier/cookiecutter); you
  don't need rendering, you need *managed file lifecycle*.

---

## Anti-patterns observed across the OSS landscape (do not repeat)

1. **Hard-coded encryption key in source** — algo-bot's `_CUSTOM_SECRET`. Every secret-management
   OSS treats KEKs as bootstrap config; never compile-time.
2. **Sandbox-disabled by default for convenience** — algo-bot's codex flag. OSS workflow engines
   (Temporal/Hatchet) sandbox by default; opt-out is the surprising path.
3. **Trust boundary inside the framework** — frameworks that mix user input and tool output
   in the same string before policy check (early LangChain). claw's `Principal` + 4-gate
   WorkflowToolBridge avoids this.
4. **Schema migrations as side-effects of process start** — survivable but unsafe. LangGraph's
   explicit `.setup()` is the better pattern.
