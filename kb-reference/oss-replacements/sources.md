# Sources — Per-Candidate Snapshot Cards

One card per candidate. `last_checked_on: 2026-05-28`. Verify stars/license live before acting.

Maturity legend: `demo / experimental / curated / production`.
Fit legend: `direct-fit / adapt / reference-only / avoid`.

---

## L0 — Channels (`MessagingGateway` adapters)

### slack-bolt-python
- `source_type`: official SDK
- `repo`: github.com/slackapi/bolt-python
- `license`: MIT
- `maturity`: production
- `claim`: covers verify_inbound (signing secret), parse_inbound (events API), send (Web API).
- `fit`: direct-fit for `adapters/channels/slack/`
- `evidence`: Slack-maintained, used by Slack itself.

### microsoft/botbuilder-python
- `source_type`: official SDK (multi-channel)
- `repo`: github.com/microsoft/botbuilder-python
- `license`: MIT
- `maturity`: production
- `claim`: abstracts Teams / Slack / Telegram / Webchat behind one Activity model — closest OSS
  parallel to your MessagingGateway concept.
- `fit`: reference-only (semantics overlap; importing it would drag a heavy SDK into your thin gateway).
  Copy the Activity-shape lesson, don't import.

### standardwebhooks (svix-libs)
- `source_type`: spec + reference impl
- `repo`: github.com/standard-webhooks/standard-webhooks
- `license`: MIT
- `maturity`: curated
- `claim`: standardised webhook signing (HMAC-SHA256, timestamped, replay-protected); constant-time
  compare; Python lib `standardwebhooks`.
- `fit`: **conditional** — only fits when *both ends* follow Standard Webhooks (e.g. you build
  outbound webhooks from claw, or you integrate with Resend/Clerk/Vercel etc. that adopted
  the spec). For Slack/Telegram/SeaTalk/WeChat the signing format is proprietary — use the
  channel's own SDK or hand-write to their spec.
- `non_fit_examples`: Slack signing v0 (different header layout); Telegram (no HMAC, uses token);
  WeChat (signature = SHA1(sort([token, ts, nonce])))

### python-telegram-bot / py-cord / discord.py
- `fit`: direct-fit per channel, same role as slack-bolt for their respective IMs.

### Chatwoot / Botpress / Tiledesk
- `fit`: **avoid** — full chatbot platforms with their own DB, UI, conversation engine. Conflict
  with claw_engine's thin-gateway philosophy.

---

## L1 — Session Dispatch (`ConversationService`)

> **Correction (2026-05-28)**: L1 was originally labelled "Orchestration" — this was wrong.
> L1 does **not** orchestrate the agent loop. Multi-turn state lives inside the L2 agent CLI
> (codex `--continue` / claude `--resume`). L1 only does:
> (a) `(workspace_id, channel, external_thread_key) → backend_thread_id` lookup;
> (b) message_id dedup; (c) max_rounds counter.
> Therefore **no OSS applies** at this layer. Frameworks that *would* fit "orchestration"
> (LangGraph / Letta / Haystack agents) are all **competing engines**, not adapters.

### LangGraph
- `source_type`: framework (listed for completeness)
- `repo`: github.com/langchain-ai/langgraph
- `license`: MIT
- `maturity`: production
- `fit`: **avoid at L1**. LangGraph wants to own the agent loop; claw delegates that to the
  backend CLI. Checkpointer schema is *not* a fit for claw's Session either (see L5 correction).

### Letta (formerly MemGPT)
- `repo`: github.com/letta-ai/letta
- `license`: Apache-2.0
- `fit`: **avoid**. Assumes summarisation + long-term memory are *its* problem; claw's Session
  stores only the backend handle. Drag-in would explode the session model.

### What L1 *would* benefit from if it grew
- `limits` (PyPI) for rate-limiting if `max_rounds` becomes per-time-window — currently overkill.
- Otherwise: nothing. L1 is intentionally thin.

---

## L2 — Agent Runtime (`CodeAgentBackend`)

> Core abstraction. No outsourcing. Listed candidates are **additional backends**, not replacements.

### aider
- `repo`: github.com/Aider-AI/aider
- `license`: Apache-2.0
- `maturity`: production
- `fit`: direct-fit as a 3rd backend (`adapters/backends/aider/`). Has stdin/stdout CLI shape
  similar to codex.

### OpenHands (ex-OpenDevin), gptme, continue-cli
- `fit`: direct-fit each, same pattern. Each one running the shared contract suite proves
  contract universality.

### claude-agent-sdk / openai-agents-sdk
- `fit`: reference-only — for a future *in-process* backend (no subprocess), wrap these instead
  of CLI.

---

## L3 — Workflows (`WorkflowService`, executors)

### Hatchet
- `source_type`: open-source product
- `repo`: github.com/hatchet-dev/hatchet
- `license`: MIT
- `maturity`: production (V1 SDK shipped; explicitly markets itself as "drop-in for Temporal/DBOS")
- `claim`: durable execution, persistent run history (matches your `WorkflowRun.progress`), Python
  V1 SDK uses Pydantic models for typed inputs, `aio_*` async naming.
- `fit`: direct-fit at `adapters/workflows/hatchet/`. Backend deps: Postgres.

### Temporal
- `repo`: github.com/temporalio/temporal
- `license`: MIT
- `maturity`: production
- `fit`: reference-only — capability superset of what claw needs; ops cost (multi-component cluster)
  hard to justify until cross-DC durability is in scope.

### Inngest
- `license`: Apache-2.0 (open-source variant)
- `fit`: adapt — TS-first; Python SDK newer. Use if team already runs JS workflows.

### arq / dramatiq / Celery
- `fit`: adapt for *task queue* only; no durable workflow concept. Need to hand-roll progress / rerun.
  Useful as the executor inside `ThreadWorkflowExecutor` if you want process-level workers without
  Hatchet's footprint.

### Prefect / Dagster
- `fit`: avoid — data-pipeline framing, wrong cognitive model for chatbot workflows.

---

## L4 — Context (`ConfigProvider`, `SecretProvider`)

### pydantic-settings
- `repo`: github.com/pydantic/pydantic-settings
- `license`: MIT
- `maturity`: production
- `claim`: layered settings (env files, env vars, secret files) with typed validation.
- `fit`: **implementation aid, not replacement**. pydantic-settings layers by *source type*
  (env vars / .env / secret files), not by claw's *organisational dimensions*
  (global ⊕ workspace ⊕ user ⊕ secret). The merge logic is claw business and stays.
  Use pydantic-settings v2 to **define the schema of `ResolvedWorkspace.env`** and type-check
  the merged result — that's the win.

### dynaconf
- `license`: MIT
- `fit`: alternative to pydantic-settings for the same schema-validation role; richer formats
  (TOML/YAML/Vault). Same caveat: doesn't replace the workspace×user merge.

### Hydra
- `license`: MIT
- `fit`: avoid — overkill for chatbot env merging (built for ML experiment configs).

### Infisical (self-hosted)
- `repo`: github.com/Infisical/infisical
- `license`: MIT (community), ~12.7k+ stars
- `maturity`: production
- `claim`: secret management with PostgreSQL + Redis backend; modern DX; lighter ops than Vault.
- `fit`: direct-fit for `adapters/secrets/infisical/` — fits claw's "secrets are just env vars at
  injection time" model. No dynamic secrets / transit encryption (you don't need them V1).

### HashiCorp Vault
- `license`: BSL (post-IBM-acquisition; not OSI-open)
- `maturity`: production
- `fit`: adapt — keep as option for orgs already on Vault; new deploys prefer Infisical or
  OpenBao (Vault fork under MPL-2.0).

### OpenBao
- `repo`: github.com/openbao/openbao
- `license`: MPL-2.0
- `fit`: adapt — Vault MPL-2.0 fork, drop-in API. Use if you need Vault's dynamic-secret features
  without BSL.

### detect-secrets / gitleaks
- `fit`: direct-fit as CI gate (catches "credentials hard-coded in SKILL.md" failure mode that
  burned algo-bot, see `algo-bot-security-gaps`).

---

## L5 — Persistence (`SessionStore`)

### SQLAlchemy 2.x (async)
- `license`: MIT
- `maturity`: production
- `fit`: direct-fit. Rewrite `SqliteSessionStore` against SQLAlchemy async, then MySQL/Postgres
  variants are configuration, not code.

### SQLModel
- `license`: MIT
- `fit`: adapt — Pydantic + SQLAlchemy wedding. Good DX, but ties session schema to Pydantic
  layer; trade-off for engine purity.

### LangGraph Checkpointer (`langgraph-checkpoint`, `-sqlite`, `-postgres`)
- `repo`: github.com/langchain-ai/langgraph (`libs/checkpoint*`)
- `license`: MIT
- `maturity`: production
- `fit`: **not applicable (corrected 2026-05-28)**. Checkpointer stores full agent-loop state
  blobs because LangGraph *owns* the agent loop. claw's Session stores **one row**:
  `(workspace_id, channel, external_thread_key) → backend_thread_id` + max_rounds counter.
  Turn history lives in the backend CLI's own session. There is nothing to borrow at the
  schema level — the abstractions don't match.

### Redis + redis-om
- `fit`: adapt — useful for hot-session cache layer (TTL on backend_thread_id lookup) but you
  still need durable store.

### Letta memory store
- `fit`: avoid (see L1).

---

## ⟂ Identity / RBAC

### Casbin (PyCasbin)
- `repo`: github.com/casbin/pycasbin
- `license`: Apache-2.0
- `maturity`: production
- `claim`: PERM (policy/effect/request/matcher) metamodel. RBAC + ABAC + ReBAC all in YAML/CSV.
  Embedded library — zero ops.
- `fit`: **direct-fit** to replace all three `can_access_workspace / can_use_skill /
  can_run_workflow` decision functions. Map your default-allow + denylist semantics as a Casbin model.

### OpenFGA
- `repo`: github.com/openfga/openfga
- `license`: Apache-2.0 (CNCF sandbox)
- `maturity`: production
- `claim`: Google Zanzibar-inspired ReBAC; sub-ms checks; service architecture.
- `fit`: adapt — pick this over Casbin when workspace ↔ user ↔ skill becomes a real graph (e.g.
  "users in group X can access skills in folder Y owned by team Z"). Extra service to operate.

### Cerbos
- `repo`: github.com/cerbos/cerbos
- `license`: Apache-2.0
- `maturity`: production
- `claim`: centralised policy decision point; YAML policy with attribute conditions; sidecar.
- `fit`: adapt — strongest when policies need to be versioned & reviewed independently of code.
  Overkill for V1.

### Keycloak / Authentik / Logto / Ory Kratos
- `license`: Apache-2.0 / MIT / various
- `fit`: **conditional**. Fits when a channel actually delivers an OIDC token (web portal, API
  gateway). For **IM channels (Slack/Teams/Telegram/SeaTalk)** the user is already authenticated
  by the IM; `resolve_user` is just a mapping-table lookup against your own DB — no OSS needed.
  Pick **Authentik** or **Logto** for lightest ops when an OIDC server is actually required.

---

## ⟂ Observability (`Tracer` seam)

### OpenTelemetry Python SDK
- `repo`: github.com/open-telemetry/opentelemetry-python
- `license`: Apache-2.0
- `maturity`: production
- `claim`: industry-standard span / trace API; OTLP exporter built in.
- `fit`: **direct-fit**. Re-cast `Tracer` Protocol so it produces OTel spans; `TraceDims` become
  span attributes; `NoOpTracer` keeps working unchanged.

### Langfuse
- `repo`: github.com/langfuse/langfuse
- `license`: MIT (core)
- `maturity`: production
- `claim`: LLM-native trace UI; **runs an OTLP HTTP endpoint at `/api/public/otel`** (HTTP/JSON
  + HTTP/protobuf; no gRPC); SDK v4 is OTel-native.
- `fit`: direct-fit downstream of an OTel-ized Tracer. **You don't need a Langfuse-specific
  adapter** if you export via OTLP — just point exporter at Langfuse.

### Arize Phoenix
- `repo`: github.com/Arize-ai/phoenix
- `license`: Elastic-2.0
- `fit`: adapt — local dev / eval-heavy use; consumes OTel.

### Helicone, LangSmith, Traceloop OpenLLMetry
- `fit`: all consume OTel — pick by team preference, no claw-side adapter needed once OTel is in.

---

## ⟂ Skills (`SkillProvisioner`)

### copier / cookiecutter
- `fit`: reference-only — solves template rendering, not your "manifest-tracked sync + revocation"
  problem.

### git sparse-checkout / git submodule
- `fit`: adapt — viable for the `adapters/skills/git/` source. Sparse-checkout matches "pull
  only the allowed skills" well.

### No full-stack OSS skill-provisioning framework exists.
- **Keep self-built.** Your manifest + revocation + path-traversal hardening is unique to the
  agentic-skills space.

---

## Cross-cutting tooling worth a card

### svix-libs (also Standard Webhooks impl)
- Already cited in L0.

### detect-secrets, gitleaks, trufflehog
- CI scanners to prevent secret-in-SKILL.md class of bug.

### testcontainers-python
- License MIT; production. Use to run real Postgres / Redis / Hatchet in tests of new adapters
  so the contract suite stays honest.
