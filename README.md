# claw_engine

A business-agnostic engine for building bots and workbenches on top of code-agent CLIs (codex / claude code / future). Same engine produces a SeaTalk algo bot or a factory B2B workbench — only adapters change.

**Status:** V1 (178 tests, ruff clean, engine layer enforces zero CLI/business literals).

---

## What it is

A Python engine that wraps code-agent CLIs (codex-cli, claude code cli) with the governance you actually need to ship a real product:

- **Dual backend, unified contract** — codex and claude are interchangeable behind one `CodeAgentBackend` Protocol. The same contract test suite verifies both.
- **Session continuity** — `(workspace_id, channel, external_thread_key)` keyed sessions persist across process restarts (sqlite).
- **Workspace = isolation + RBAC boundary** — each workspace has its own `cwd`, merged env (global + workspace + user + secrets), allowed_skills, allowed_workflows.
- **Secret never leaks** — secrets flow into the agent subprocess env, never into trace, never into skill source. Enforced by tests, not convention.
- **Skill provisioning with real revocation** — only `allowed_skills ∩ can_use_skill` get synced to `.agents/skills`; revoked skills are removed from disk on next provision; path-traversal proof.
- **Workflow as a tool** — long-running async workflows are first-class; `WorkflowToolBridge` lets an agent trigger them via RBAC-checked calls, with cross-workspace isolation.
- **Observability seam** — span-style `Tracer` (NoOp / in-memory / Langfuse-adapter-ready) with full `{workspace_id, user_id, session_id, backend_name}` dimensions. Trace input is redacted before it ever touches the tracer.
- **Security-first invariants, test-enforced** — every layer's permission/redaction boundary has at least one test, not just documentation.

---

## Quick start

```bash
# 1. Install (Python 3.11+)
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 2. Run the CLI smoke (uses the built-in echo backend, no real CLI needed)
.venv/bin/python -m claw_engine --backend fake --prompt hello
# -> echo: hello

# 3. Run the test suite
.venv/bin/pytest -q
# -> 178 passed
```

For a full programmatic walkthrough (identity → workspace → engine → workflow → bridge → trace), see **[docs/DEMO.md](docs/DEMO.md)**.

---

## Architecture in one paragraph

The engine is **layered** (L0 channels → L1 orchestration → L2 agent runtime → L3 capabilities → L4 context → L5 persistence) with **cross-cutting** identity, observability, and skills. Everything business-specific lives behind one of **5 plug-in seams** (`MessagingGateway`, `PromptProvider`, `WorkspaceDataSource`, `CodeAgentBackend`, `IdentityProvider`). The engine package contains only contracts and reusable infrastructure; concrete backends (codex, claude), channels (webhook), and any future real source/IM/store live under `adapters/`. A CI-enforced purity test guarantees no business/CLI literals (`seatalk`, `jira`, `codex`, `claude`, …) ever appear inside `engine/`. Full detail in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## Project layout

```
claw_engine/
├── engine/                   # business-zero-knowledge core (purity-gated)
│   ├── runtime/              # L2: CodeAgentBackend contract, AgentEvent
│   ├── orchestration/        # L1: Engine.run_turn, ConversationService, WorkspaceConversationGateway
│   ├── channels/             # L0: MessagingGateway contract, ChannelRunner, IdentityWorkspaceRouter
│   ├── context/              # L4: ConfigProvider, SecretProvider, WorkspaceResolver
│   ├── persistence/          # L5: SessionStore (memory + sqlite)
│   ├── identity/             # ⟂  IdentityProvider, User, Principal
│   ├── observability/        # ⟂  Tracer seam (NoOp + Memory; Langfuse via adapter)
│   ├── skills/               # ⟂  SkillSource, SkillProvisioner (manifest-backed revocation)
│   └── workflows/            # L3 WorkflowService, WorkflowToolBridge, executor
├── adapters/                 # everything CLI/business-specific
│   ├── backends/             #   codex/ , claude/, shared spawn+errors
│   └── channels/             #   webhook/
├── tests/
│   ├── contract/             # cross-impl contract suites (backend × {codex,claude}, store × {memory,sqlite})
│   └── purity/               # CI gate: engine/ never imports adapters; never names a CLI/business
└── docs/
    ├── ARCHITECTURE.md       # layered architecture + seams + dependency rules
    ├── DEMO.md               # full-chain walkthrough
    └── superpowers/          # specs and plans (auditable history)
```

---

## Extension points (the 5 seams)

| Seam | Contract | V1 impl in this repo | Add a new one by |
|---|---|---|---|
| `MessagingGateway` | `engine/channels/contracts.py` | `adapters/channels/webhook.py` | Implementing `verify_inbound / parse_inbound / send_text / send_attachments / start_progress / update_progress` |
| `CodeAgentBackend` | `engine/runtime/contracts.py` | `adapters/backends/codex/`, `adapters/backends/claude/` | Translating your CLI's event stream into the unified `AgentEvent`; running the shared backend contract suite against it |
| `IdentityProvider` | `engine/identity/contracts.py` | `engine/identity/memory.py` (InMemory; SSO/LDAP in adapters) | Implementing `resolve_user / authorized_workspaces / can_access_workspace / can_use_skill / can_run_workflow` |
| `WorkspaceDataSource` | (reserved; V1 uses path-based cwd) | — | (Future: git sync / CSV ingest / etc.) |
| `PromptProvider` | (reserved) | — | (Future: per-workspace prompt assembly) |

The **dependency rule** is one-way and CI-enforced: `adapters` may import `engine`; `engine` never imports `adapters`. See `tests/purity/`.

---

## Testing

```bash
# Full suite
.venv/bin/pytest -q

# Layered slices
.venv/bin/pytest tests/contract -q          # cross-impl contracts (backends, sessionstore)
.venv/bin/pytest tests/purity -q            # engine/adapters boundary

# Lint
.venv/bin/ruff check claw_engine tests
```

Two contract suites are reusable when you add a new backend or session store:

- **Backend contract** (`tests/contract/test_backend_contract.py`) — same 5 behavioral cases run against codex and claude fixtures; add your fixture to verify a new backend.
- **SessionStore contract** (`tests/contract/test_sessionstore_contract.py`) — same 4 cases run against memory and sqlite.

---

## Security invariants (test-locked)

| Invariant | Where | Test |
|---|---|---|
| Secret never enters trace | `Engine.run_turn` only passes `dims + prompt + trace_metadata` to tracer; never `env` or backend `metadata` | `test_engine_does_not_leak_env_or_backend_metadata_to_tracer` |
| Workspace_id can't path-traverse | `engine/context/workspace.py:_validate_workspace_id` | `test_path_traversal_workspace_id_rejected` |
| Skill name can't path-traverse | `engine/skills/source.py:validate_skill_name` | `test_invalid_skill_names_rejected_no_read` |
| Revoked skills are removed from disk | `SkillProvisioner` manifest + `revoked` bucket | `test_revoked_skill_is_removed_from_disk` |
| Manifest tampering can't escape `dest_root` | `_read_manifest` validates entries + rmtree realpath-contained | `test_forged_manifest_with_path_traversal_does_not_escape_dest` |
| Inbound webhook must verify before engine | `ChannelRunner.handle_raw` calls `verify_inbound` first | `test_bad_signature_blocks_loop_no_backend_no_reply` |
| Unauthorized workspace doesn't run a turn | `IdentityWorkspaceRouter` raises `WorkspaceAccessDenied`; `ConversationService` not reached | `test_unauthorized_workspace_denied_no_backend_no_reply` |
| Revoked workflow can't be read via old run_id | `WorkflowToolBridge.get` re-validates current permissions | `test_get_after_workflow_removed_from_allowed` |

---

## Status & what's next

V1 ships as a hermetic, in-memory + sqlite + local-fs implementation. All transports that need network (real Langfuse, MCP server for workflow tools, git skill source, MySQL session store, SSO/LDAP identity) are designed as **adapter slots** with passing contract tests, ready to be wired without touching the engine.

Roadmap candidates:
- `adapters/observability/langfuse.py` — implement `Tracer` against the Langfuse SDK.
- `adapters/workflows/mcp_transport.py` — expose `WorkflowToolBridge` over MCP so an agent CLI can call it.
- `adapters/skills/git_source.py` — `SkillSource` backed by a git repo, periodic sync.
- `adapters/persistence/mysql_store.py` — production `SessionStore`.

---

## Documents

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — layered architecture, dependency rules, contract testing.
- **[docs/DEMO.md](docs/DEMO.md)** — runnable end-to-end walkthrough.
- **[docs/superpowers/specs/](docs/superpowers/specs/)** — the original design spec.
- **[docs/superpowers/plans/](docs/superpowers/plans/)** — auditable per-phase implementation plans with all review hardenings.
