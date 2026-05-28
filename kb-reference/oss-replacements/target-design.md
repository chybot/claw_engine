# Target Design — claw_engine OSS Adoption Recommendation

Concrete plan per seam, ordered by **return on effort**. Each item lists the seam, the OSS pick,
the adapter package path, effort estimate, and the test contract that proves the swap.

## Five-category framing (introduced 2026-05-28 after L1 mis-attribution review)

Reading the table below: every row maps a claw module/sub-capability to one category.

- 🟢 **Module replacement** — adapter swaps in OSS; engine Protocol unchanged
- 🔵 **Implementation aid** — OSS provides typing/utility; business logic stays
- 🟡 **Concept reference only** — read the docs; do **not** import
- ⚪ **No outsource** — intentionally thin, business-specific, or core abstraction
- 🔧 **CI / dev tooling** — not a module replacement; a process guard

Things wrongly classified in the first draft and corrected here:

- **L1 ConversationService** → ⚪ (not 🟢). Not an orchestrator; multi-turn lives in L2 backend CLI.
- **L4 `LayeredConfigProvider`** → ⚪ (the merge); pydantic-settings is 🔵 for the *resulting schema*.
- **L5 LangGraph checkpointer borrowing** → removed entirely. Wrong abstraction.
- **Standard Webhooks** → conditional, not generic. Per-IM SDK is 🟢; standardwebhooks is 🔵 only.
- **Identity SSO** → conditional; ⚪ for IM channels, 🟢 only for web/OIDC channels.

---

## Tier-1 — High leverage, do before P7 (SeaTalk portability proof)

These three give the biggest credibility & feature lift with the smallest blast radius.

### 1. OpenTelemetry-ize the `Tracer` seam

| | |
|---|---|
| **Seam** | `engine/observability/Tracer`, `TraceSpan`, `TraceDims` |
| **OSS pick** | `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` |
| **Adapter path** | `adapters/observability/otel/` |
| **Effort** | ~½ day (Protocol shape barely changes; add OtelTracer impl + exporter wiring) |
| **Test contract** | Existing `MemoryTracer` contract suite still passes. Add new contract test: `OtelTracer` emits one OTLP span per turn with attributes `claw.workspace_id, claw.user_id, claw.session_id, claw.backend_name` and zero `env.*` or `metadata.*` keys (proves redact-before-tracer invariant held). |
| **Why first** | Langfuse (already planned) becomes free via OTLP. Same change unlocks Phoenix / Helicone / LangSmith / Datadog / vendor X. Zero-vendor-lockin observability is a strong selling point. |
| **Invariant preserved** | redact-before-tracer is engine-side; OTel only serialises what engine hands over. |
| **Migration step** | Keep current `Tracer` Protocol. Add `OtelTracer(adapter)` that wraps `opentelemetry.trace.Tracer`. Existing `NoOpTracer` / `MemoryTracer` unaffected. |

### 2. Casbin for `can_*` decisions

| | |
|---|---|
| **Seam** | `IdentityProvider.can_access_workspace`, `can_use_skill`, `can_run_workflow` |
| **OSS pick** | `casbin` (Apache-2.0, embedded; no extra process) |
| **Adapter path** | `adapters/identity/casbin/` (new) — or `engine/identity/` can grow a `CasbinPolicy` strategy if we want it default. Recommend adapter to preserve purity. |
| **Effort** | ~1 day (write 1 Casbin model file covering 3 decisions, port denylist semantics, contract test) |
| **Test contract** | Existing identity contract suite passes against new `CasbinIdentityProvider` backed by a YAML policy file. Add: revoked-skill → can_use_skill returns False (already covered); cross-workspace skill → False; default-allow when no deny rule. |
| **Why** | Replaces 3 hand-coded predicates + InMemory `denied_skills` with a single declarative policy. Auditable. Hot-reloadable. Adds RBAC + ABAC + ReBAC at zero extra cost when needed. |
| **Invariant preserved** | RBAC stays as engine-checked gates; only the *decision function* is delegated. `WorkflowToolBridge`'s 4-gate order is untouched. |

### 3. SQLAlchemy 2.x async for `SqliteSessionStore` (🔵 implementation aid)

| | |
|---|---|
| **Seam** | `engine/persistence/SessionStore` |
| **OSS pick** | `sqlalchemy[asyncio]` (MIT) + `aiosqlite` + (future) `asyncpg` / `aiomysql` |
| **Adapter path** | rewrite `engine/persistence/sqlite.py` → split: `engine/persistence/` keeps `Session` + `SessionStore` Protocol; `adapters/persistence/sqlalchemy/` holds the impl. One impl serves sqlite + mysql + postgres. |
| **Effort** | ~1 day (rewrite + run existing memory+sqlite contract suite + add postgres run via testcontainers) |
| **Test contract** | Existing shared `SessionStore` contract suite (memory + sqlite) extends to (memory + sqlalchemy-sqlite + sqlalchemy-postgres). All pass identically. |
| **Why** | P7 SeaTalk portability proof needs mysql. SQLAlchemy turns that into a connection-string change. ORM also kills the "did I parametrise this query?" review tax. |
| **Schema** | Stays one table (`sessions`). **Do not** copy LangGraph checkpointer's `checkpoints`+`blobs` split — that schema exists because LangGraph stores full agent loop state; claw stores only the backend handle (turn history lives in the backend CLI's own session). Wrong abstraction. |
| **Invariant preserved** | `engine/persistence/` Protocol stays pure dataclasses; SQLAlchemy types live in `adapters/`. Purity gate still passes. |

---

## Tier-2 — Do when scaling out

### 4. Hatchet for durable workflow execution

| | |
|---|---|
| **Seam** | `engine/workflows/WorkflowExecutor`, indirectly `WorkflowStore` |
| **OSS pick** | `hatchet-sdk` (MIT) |
| **Adapter path** | `adapters/workflows/hatchet/` |
| **Effort** | ~3 days (Postgres setup, sdk wiring, contract test for cross-process resumption) |
| **Trigger condition** | First time a workflow needs to survive a process restart, or first time you want a UI for in-flight runs. Don't do it sooner — Hatchet adds a Postgres dependency. |
| **Test contract** | Existing `WorkflowService` contract passes against `HatchetWorkflowExecutor`. New test: rerun after process kill resumes. **Critical**: `WorkflowToolBridge`'s 4 RBAC gates run in engine, not delegated to Hatchet permissions. |

### 5. Infisical for secrets

| | |
|---|---|
| **Seam** | `engine/context/SecretProvider` |
| **OSS pick** | `infisical-python` against self-hosted Infisical (MIT, Postgres+Redis) |
| **Adapter path** | `adapters/secrets/infisical/` |
| **Effort** | ~1 day (sdk wiring, deployment of Infisical via docker-compose for tests) |
| **Trigger condition** | First time secrets need to be rotated, audited, or accessed from >1 machine. |
| **Why Infisical over Vault** | MIT vs BSL; PostgreSQL + Redis instead of Vault's Raft cluster; matches claw's "secret = env var at injection" model. If you specifically need Vault's dynamic-secret / transit-encryption features, use **OpenBao** (MPL-2.0 Vault fork). |
| **Invariant preserved** | `SecretProvider` redact behaviour stays in engine; adapter only fetches. |

### 6. standardwebhooks for HMAC (🔵 — conditional)

| | |
|---|---|
| **Seam** | `adapters/channels/webhook.py::verify_inbound` |
| **OSS pick** | `standardwebhooks` (Python lib) |
| **Adapter path** | edit existing generic webhook adapter only |
| **Effort** | ½ day |
| **Scope** | **Only** when claw is on both ends (outbound webhooks from claw) or integrating with a provider that adopted the Standard Webhooks spec (Resend / Clerk / Vercel). |
| **Not applicable to** | Slack (proprietary signing v0), Telegram (no HMAC), WeChat (SHA1 of sorted token+ts+nonce), SeaTalk (proprietary). Each goes through that IM's official SDK in its own adapter — see Tier-3 #9. |
| **Why** | When applicable, removes a class of timing-attack and header-normalisation bugs. |

### 7. Aider as a 3rd backend

| | |
|---|---|
| **Seam** | `engine/runtime/CodeAgentBackend` |
| **OSS pick** | `aider-chat` (Apache-2.0) |
| **Adapter path** | `adapters/backends/aider/` |
| **Effort** | ~2 days (parse aider stdout into AgentEvent stream; supply contract suite) |
| **Why** | Contract-suite weight goes from 5×2 → 5×3. Strong narrative for "engine works with any code-agent CLI". |

### 8. pydantic-settings for **schema validation** of `ResolvedWorkspace.env` (🔵)

| | |
|---|---|
| **Seam** | `engine/context/ResolvedWorkspace.env` (the *output* of the merge, not the merge itself) |
| **OSS pick** | `pydantic-settings` v2 (or plain `pydantic.BaseModel` if you don't need env-source layering on top) |
| **Adapter path** | Define typed env schema in `engine/context/` (Pydantic model is pure dataclass-ish; OK for engine); validate after `LayeredConfigProvider` produces the merged dict. |
| **Effort** | ~½ day |
| **What stays** | `LayeredConfigProvider`'s `global ⊕ workspace ⊕ user ⊕ secret` merge — this is claw business semantics, **not** what pydantic-settings does. pydantic-settings layers by *source type* (env vars / .env / secret files), a different axis. Keep the merge. |
| **Why** | Catches config typos and type errors at workspace resolve time, not at backend exec time. |

---

## Tier-3 — Optional / situational

### 9. IM SDKs per channel

When you actually ship a real Slack / Teams / Telegram channel, lean on the official SDK in that
adapter. Don't pre-build adapters.

| Channel | SDK | Adapter |
|---|---|---|
| Slack | `slack-bolt-python` (MIT, official) | `adapters/channels/slack/` |
| Teams | `botbuilder-python` (MIT, official) | `adapters/channels/teams/` |
| Telegram | `python-telegram-bot` (LGPL-3.0) | `adapters/channels/telegram/` |
| Discord | `py-cord` (MIT) | `adapters/channels/discord/` |
| SeaTalk | (proprietary; build against their HTTP API + standardwebhooks for verify) | `adapters/channels/seatalk/` |

### 10. detect-secrets / gitleaks CI gate

Add to CI to prevent the algo-bot-class regression where credentials end up in `SKILL.md`. Pre-commit
hook + GitHub Action are both fast.

### 11. Authentik or Logto for `IdentityProvider.resolve_user` — **only for web/API channels**

When the channel actually delivers an OIDC token (web portal, API gateway). `adapters/identity/oidc/`
consumes the ID token; Casbin (#2) still handles `can_*` decisions.

**Skip for IM channels** (Slack/Teams/Telegram/SeaTalk): the IM already authenticated the user;
`resolve_user` is a mapping-table lookup `(channel, raw_user_ref) → User` against your own DB.
Pulling in an OIDC server here is over-engineering.

---

## What to refuse

### Do not adopt — would violate engine purity or seam philosophy

| OSS | Why refused |
|---|---|
| **LangGraph / Letta / Haystack as orchestrator** | Compete with `engine/runtime` ownership; their "graph drives the agent" model conflicts with claw treating agents as subprocess black boxes. |
| **LangGraph checkpointer as Session schema reference** | Stores full agent loop state because the framework owns the loop. claw stores one row (a handle to the backend's own session). Wrong abstraction; nothing to borrow. |
| **standardwebhooks as a generic signature lib** | Only valid when both ends adopt the Standard Webhooks spec. Slack/Telegram/WeChat/SeaTalk each use proprietary signing — go through their SDK. |
| **Keycloak/Authentik for IM-only deployments** | `resolve_user` for IM channels is a DB lookup; an OIDC server is over-engineering. |
| **pydantic-settings as `LayeredConfigProvider` replacement** | pydantic-settings layers by source type (env / .env / secret file), not by claw's organisational dimensions (workspace × user). Use it for schema validation of the *merged result*, not the merge itself. |
| **Chatwoot / Botpress / Tiledesk** | Full chatbot platforms — fuse verify/parse/route/respond/UI; remove the seam. |
| **HashiCorp Vault (default)** | BSL licence; heavy ops; only use when org already runs it or when dynamic-secret feature is required. Prefer Infisical or OpenBao. |
| **Celery as workflow** | Concept regression — task queue ≠ durable workflow; you'd re-hand-roll progress/rerun on top. |
| **Copier / Cookiecutter for skills** | Wrong abstraction — they render templates; you need managed file lifecycle with revocation. |
| **LangChain SQL chains / vector DB integrations** | Out of scope and would drag a large dependency graph into engine. |

---

## Proposed plan-of-record sequence

If you adopt this KB, the suggested plan numbering (continuing from V1's commit `759444a`):

```
P7  SeaTalk portability proof + Tier-1 OSS adoption
     ├── P7a  OpenTelemetry-ize Tracer (#1)
     ├── P7b  Casbin can_* (#2)
     ├── P7c  SQLAlchemy SessionStore (#3) + mysql/postgres contract tests
     └── P7d  factory_b2b smoke + engine/ git-diff=0 verification

P8  Scale-out adapters (Tier-2)
     ├── P8a  Hatchet executor (#4)
     ├── P8b  Infisical secrets (#5)
     ├── P8c  standardwebhooks + aider backend (#6, #7)
     └── P8d  pydantic-settings config (#8)

P9  Per-channel real adapters (Tier-3, demand-driven)
     └── slack/teams/seatalk as they ship
```

Each Pn-x can land independently and is reversible (adapter swap, no engine change).

---

## Risk register

| Risk | Mitigation |
|---|---|
| OTel SDK churn / GenAI semantic conventions still evolving | Use `claw.*` attribute namespace as stable layer; map to `gen_ai.*` only where convention is finalised. |
| Casbin policy file becomes a hidden third config surface | Document policy file as part of workspace config; load it via `ConfigProvider`, not a separate path. |
| SQLAlchemy migrations need an Alembic dependency | Use `.setup()`-style idempotent `CREATE TABLE IF NOT EXISTS` for V1; defer Alembic to schema-v2. |
| Hatchet's Postgres dep added too early | Gate behind a config flag; `InlineExecutor` stays the dev/test default. |
| Infisical lacks dynamic secrets | Confirmed acceptable for V1; if you need them later, switch adapter to OpenBao. |
| Adopting OSS leaks vendor names into `engine/` (purity gate fail) | Purity CI test already blocks this — verified at commit `759444a`. Adapter naming convention reinforces. |

---

## Linkage back to claw_engine memories

- Reflects decisions captured in `[[claw-engine-project]]` (V1 architecture, 5 seams, purity gate).
- Addresses every must-fix from `[[algo-bot-security-gaps]]`:
  - 🔴 webhook unauth → standardwebhooks (#6)
  - 🔴 hard-coded encryption key → Infisical (#5) + detect-secrets CI (#10)
  - 🔴 plaintext credentials in skills → detect-secrets/gitleaks pre-commit (#10)
  - 🟠 sandbox-disabled → out of scope here (backend-side concern; track separately)
