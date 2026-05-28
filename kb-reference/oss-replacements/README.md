# claw_engine — OSS Replacement Survey (KB)

A task-specific knowledge base produced via the **skill-forge** distillation workflow.
Scope: for each claw_engine layer / seam, identify mature open-source frameworks that could
replace or augment the current hand-rolled implementation, with explicit
copy / adapt / avoid judgments.

> Produced 2026-05-28. Anchors against the architecture at commit `759444a` (V1, 178 tests).
> Future readers: re-verify upstream stars/licences/maturity before acting; see `sources.md`.

---

## 1. Target Definition (skills-forge §1)

| Field | Value |
|---|---|
| `target_problem` | Which claw_engine modules can be outsourced to battle-tested OSS without breaking the "engine purity" invariant (zero CLI / business literals inside `engine/`)? |
| `target_users` | claw_engine maintainer + future seam-adapter authors |
| `target_platforms` | Python 3.11+, CPython, self-hostable (no SaaS-only) |
| `target_task_scope` | L0 channels, L1 orchestration, L3 workflows, L4 context, L5 persistence, ⟂ identity, ⟂ observability, ⟂ skills. Excludes L2 backend (core abstraction, not outsourcable). |
| `non_goals` | Vendor comparison for SaaS; benchmark numbers; perf microoptimisation; replacing the 5 seams themselves (only their implementations). |

## 2. How to read this KB

- **`sources.md`** — one card per candidate (snapshot + claim + evidence pointer).
  Use when triaging "is this thing alive / serious / appropriate licence?".
- **`patterns.md`** — distilled cross-source observations, per claw_engine layer.
  Use when reasoning "what does the OSS landscape teach us about this seam?".
- **`target-design.md`** — concrete adoption recommendation for claw_engine, ordered by
  return-on-effort. **This is the action-oriented file.** Read this if you only read one.

## 3. Bottom-line recommendation (preview of `target-design.md`)

If you adopt **3 things only**, in this order:

1. **OpenTelemetry-ize the `Tracer` seam** → Langfuse / Phoenix / any OTel backend becomes a free swap.
   ([Langfuse runs an OTLP endpoint natively](https://langfuse.com/integrations/native/opentelemetry).)
2. **Replace `InMemoryIdentityProvider`'s `can_*` decision logic with Casbin** → policy becomes
   YAML, auditable, hot-reloadable; matches RBAC + ABAC + ReBAC without rewriting.
3. **Rewrite `SqliteSessionStore` against SQLAlchemy 2.x async** → free MySQL/Postgres + ORM
   safety + lets you reuse a familiar schema shape from LangGraph's checkpointer.

Tier-2 (do when scaling out):

4. Swap `ThreadWorkflowExecutor` → **Hatchet** when workflows need durability across process restarts.
5. `InMemorySecretProvider` → **Infisical** (MIT, PostgreSQL+Redis; lighter than Vault BSL).
6. Webhook signature verification → **standardwebhooks** lib for constant-time multi-format support.

**Do not outsource:** `engine/runtime` (core), `engine/skills` (too business-specific),
`MessagingGateway` Protocol (your seam — keep it). Concrete IM channels (`adapters/channels/*`)
should adopt official SDKs (slack-bolt, botbuilder, etc.) one-by-one but the contract stays yours.

## 4. Compatibility with claw_engine invariants

Every recommendation below preserves:

- **Purity gate** — OSS imports land in `adapters/` only, never `engine/`.
- **Redact-before-tracer** — Tracer adapter still consumes the engine-pre-redacted dim set.
- **Revocation-on-provision** for skills.
- **HMAC-verify-before-engine** in channels.

A recommendation that breaks any of those is in §5 "Avoid" of `target-design.md`.
