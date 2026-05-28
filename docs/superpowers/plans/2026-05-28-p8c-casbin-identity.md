# P8c: CasbinIdentityProvider Adapter — Sub-Plan

**Parent plan:** `/Users/lucas.xu/.claude/plans/cryptic-twirling-sonnet.md` § Tier-2 / P8c
**Baseline:** main @ `bd94f7d` (after PR #2 P8b merge), tag `v1.0.0`
**Branch:** `feat/p8c-casbin-identity`

---

## Why this sub-plan exists

P8c's risk is **scope creep**, not lifecycle complexity (it has neither IO nor subprocess). The
parent plan says clearly: Casbin only handles `can_use_skill` + `can_run_workflow`; `resolve_user`,
`authorized_workspaces`, `can_access_workspace` stay in the V1 user-workspace mapping table.
Implementing this requires picking a **composition pattern** that makes the boundary unambiguous,
and a **policy reload model** that doesn't drift into "implicit file watcher" territory. Lock
both up front so the implementer doesn't reinvent.

---

## 1. Composition Pattern (Decision)

`CasbinIdentityProvider` is **NOT a standalone IdentityProvider**. It **wraps** an existing
`IdentityProvider` (the "base"), delegating everything except the 2 skill/workflow `can_*`
methods to Casbin's enforcer.

```python
class CasbinIdentityProvider(IdentityProvider):
    def __init__(self, base: IdentityProvider, *, model_path: Path | str, policy_path: Path | str) -> None:
        # validate inputs (paths exist as files, Casbin can load them)
        # construct casbin.Enforcer once; cache on self._enforcer
        # base is stored for delegation
        ...
    
    # delegated to base (unchanged) ------------------------------------------
    def resolve_user(self, raw_user_ref: str) -> User:
        return self._base.resolve_user(raw_user_ref)
    
    def authorized_workspaces(self, user: User) -> tuple[str, ...]:
        return self._base.authorized_workspaces(user)
    
    def can_access_workspace(self, user: User, workspace_id: str) -> bool:
        return self._base.can_access_workspace(user, workspace_id)
    
    # delegated to Casbin (V1 InMemory `denied_skills` semantics expressed in policy) -
    def can_use_skill(self, user: User, workspace_id: str, skill: str) -> bool:
        return self._enforcer.enforce(user.user_id, workspace_id, skill, "use")
    
    def can_run_workflow(self, user: User, workspace_id: str, workflow: str) -> bool:
        return self._enforcer.enforce(user.user_id, workspace_id, workflow, "run")
    
    # adapter-local control surface (NOT in engine Protocol) -----------------
    def reload_policy(self) -> None: ...
    def add_policy(self, sub: str, ws: str, obj: str, act: str, eft: str = "deny") -> bool: ...
    def remove_policy(self, sub: str, ws: str, obj: str, act: str) -> bool: ...
```

**Why wrap, not subclass:** subclassing `InMemoryIdentityProvider` would couple the adapter to a
concrete engine class (reverse-import smell; also breaks if engine swaps the InMemory impl).
Wrapping accepts **any** `IdentityProvider`-implementing base, which means tests can compose
Casbin with any engine-side provider without modification, and prod can compose with a future
SSO-backed `IdentityProvider` (e.g. Keycloak resolve_user + Casbin can_*).

**Critical invariant:** the `base` parameter is what determines `can_access_workspace` answers.
Casbin **never** participates in workspace-access decisions in P8c. This is acceptance-critical
and tested by §6 #5.

---

## 2. Casbin Model (`model.conf`)

PERM model with default-allow + explicit-deny:

```ini
# claw_engine P8c — skill/workflow authorization model
# Decision: default-allow, deny rules override.

[request_definition]
r = sub, ws, obj, act

[policy_definition]
p = sub, ws, obj, act, eft

[policy_effect]
e = !some(where (p.eft == deny))

[matchers]
m = (r.sub == p.sub || p.sub == "*") && (r.ws == p.ws || p.ws == "*") && (r.obj == p.obj || p.obj == "*") && (r.act == p.act || p.act == "*")
```

**Field semantics:**

| Field | Type | Example | Wildcard |
|---|---|---|---|
| `sub` | `User.user_id` | `"alice"` | `"*"` matches any user |
| `ws` | `workspace_id` | `"workspace-ads"` | `"*"` matches any workspace |
| `obj` | skill name or workflow name | `"social-media-query"` | `"*"` matches any |
| `act` | `"use"` (skill) or `"run"` (workflow) | `"use"` | `"*"` matches both |
| `eft` | `"allow"` or `"deny"` | `"deny"` | n/a |

**Policy effect logic** (in plain English):
- If ANY policy rule with `eft="deny"` matches the request → deny
- Otherwise (including no matching rule at all) → allow

This matches V1 `InMemoryIdentityProvider.denied_skills` semantics: default permit, denylist
overrides. Allow rules (`eft="allow"`) are accepted by the model but redundant in default-allow
mode; they exist for future enforcement modes.

**Why not group/role inheritance (`g = _, _`)?** Out of scope for V1. The matcher above is a flat
table. If user X needs the same denies as user Y, write them twice in policy.csv. Group hierarchies
are deferred to a future plan.

---

## 3. Policy file (`policy.csv`) — example shipped

```csv
# claw_engine P8c — example policy
# Format: p, <sub>, <ws>, <obj>, <act>, <eft>
# Default behavior is allow; only put deny rules here.

# Example: alice cannot use the social-media-query skill in workspace-ads
p, alice, workspace-ads, social-media-query, use, deny

# Example: nobody can run the dangerous-cleanup workflow in workspace-prod
p, *, workspace-prod, dangerous-cleanup, run, deny

# Example: a specific user is blocked from one workflow in their default workspace
p, bob, workspace-bob, deploy-prod, run, deny
```

**The shipped `policy.csv` in the adapter package is a documented example.** Production callers
load their own. Path is passed to the constructor — no implicit search of `~/.claw_policy` or
similar.

---

## 4. Policy Reload Semantics (LOCKED — no surprises)

Per parent plan and §12 of P8b's locked decisions: **no auto-watch, no inotify, no file-modification
detection**. Three explicit control points only:

| Method | Behavior | When to use |
|---|---|---|
| `reload_policy()` | Re-reads `policy_path` from disk; replaces in-memory policy atomically | After editing `policy.csv` on disk |
| `add_policy(sub, ws, obj, act, eft="deny")` | Adds a rule to in-memory policy. Returns `True` if added (False if already present). **Does NOT write to disk.** | Programmatic deny (tests, runtime admin) |
| `remove_policy(sub, ws, obj, act)` | Removes matching rule from in-memory policy. Returns `True` if removed. **Does NOT write to disk.** | Programmatic un-deny |

**Why no auto-write to disk:** explicit control. Caller decides whether to mirror in-memory changes
back to a file. Mixing in-memory and on-disk state implicitly is a common source of "edited the
file but it didn't take effect" bugs.

**Documentation requirement:** class docstring must include this exact sentence:
> "Edits to policy.csv on disk DO NOT take effect until `reload_policy()` is called or the process
> is restarted. There is no file watcher."

This sentence is searched by an explicit test (§6 #4) to make sure the docstring stays present.

---

## 5. Engine Integration (How Callers Wire This Up)

The adapter does NOT auto-replace V1's `InMemoryIdentityProvider`. Callers explicitly compose:

```python
# In an example or production wiring:
from claw_engine.engine.identity.memory import InMemoryIdentityProvider
from claw_engine.adapters.identity.casbin import CasbinIdentityProvider

base = InMemoryIdentityProvider(
    users={"alice": User(user_id="alice", display_name="Alice", default_workspace="workspace-ads")},
    workspaces_by_user={"alice": ("workspace-ads",)},
)

identity = CasbinIdentityProvider(
    base=base,
    model_path="path/to/model.conf",
    policy_path="path/to/policy.csv",
)

# `identity` now satisfies the engine `IdentityProvider` Protocol.
# Pass it to Engine, ChannelRunner, WorkflowToolBridge, etc. as usual.
```

The engine doesn't know Casbin exists. The adapter satisfies the Protocol.

**Acceptance criterion**: `isinstance(CasbinIdentityProvider(...), IdentityProvider) == True` via
the `@runtime_checkable` Protocol check — Casbin adapter is interchangeable with `InMemoryIdentityProvider`.

---

## 6. Test Invariants (`tests/adapters/test_casbin_identity.py`)

⭐ = acceptance-critical (must pass before merge).

1. **Default allow**: empty policy.csv → `can_use_skill(alice, ws, skill) is True`, `can_run_workflow(...)` True. Mirror V1 default.

2. **Explicit deny**: policy `p, alice, ws-ads, social-media-query, use, deny` → 
   `can_use_skill(alice, ws-ads, social-media-query) is False`.
   Other skills / users / workspaces in the same workspace are still True.

3. **Wildcard deny**: policy `p, *, ws-prod, dangerous-cleanup, run, deny` → 
   any user blocked from `can_run_workflow(*, ws-prod, dangerous-cleanup)`.
   Same workflow in a different workspace is still True.

4. **Docstring contract**: read the class docstring; assert it contains the exact substring
   `"reload_policy()"` and the phrase `"no file watcher"`. Catches future doc drift.

5. ⭐ **Casbin does NOT affect workspace access**: build adapter with a base whose
   `can_access_workspace` returns specific yes/no answers; Casbin policy has no rules; verify
   `adapter.can_access_workspace(user, ws)` returns whatever base returns. Then with a Casbin policy
   that contains `p, alice, ws-X, *, *, deny` (which would block ALL skill/workflow actions);
   verify `adapter.can_access_workspace(alice, ws-X)` still returns base's answer (NOT False).

6. ⭐ **Programmatic add/remove without reload**: starting from empty policy, call
   `adapter.add_policy("alice", "ws", "skill1", "use", "deny")` → `can_use_skill(alice, ws, skill1)`
   immediately returns False. Then `remove_policy(...)` → returns True again.

7. ⭐ **Edit policy.csv WITHOUT reload → decision unchanged**: 
   - Build adapter with empty policy file
   - Verify `can_use_skill(alice, ws, foo) is True`
   - **Append `p, alice, ws, foo, use, deny\n` to the policy.csv on disk**
   - **WITHOUT calling `reload_policy`**: verify `can_use_skill(alice, ws, foo) is still True`
   - Now call `reload_policy()`: verify `can_use_skill(alice, ws, foo) is False`
   - This proves there is no implicit file watcher.

8. **Reload picks up changes**: same setup as #7 but call `reload_policy()` after the edit.
   Verify state changes as expected.

9. **Reload is atomic**: midway through a `reload_policy()` call, no thread observes a mixed
   state. (For V1 single-threaded contract this is straightforward — Casbin's `load_policy()`
   builds a new internal table before swapping; assert by mocking or by reading the source
   that there's no partial-state window. Light test; if non-trivial, defer to a `# TODO: thread-safety`
   comment and a follow-up.)

10. **Composition compatibility (Protocol check)**: 
    `isinstance(adapter, IdentityProvider) is True` (runtime_checkable Protocol).

11. **Delegation correctness**: build base with specific `resolve_user`, `authorized_workspaces`,
    `can_access_workspace` returns; assert adapter returns identical values for those 3 methods
    regardless of Casbin policy state.

12. **Bad policy file → adapter-local error**: pass `model_path` or `policy_path` that doesn't
    exist (or is malformed CSV); construction raises an adapter-local exception
    (`CasbinIdentityProviderError` or `FileNotFoundError` with clear message) — NOT a casbin-internal
    exception type leaking out. Engine-side doesn't depend on this; just hygiene.

13. **Contract suite cross-impl**: parametrise an existing identity contract suite (if one exists)
    or write a tiny one that covers the 5 `IdentityProvider` methods, run against
    `InMemoryIdentityProvider` and `CasbinIdentityProvider(base=InMemoryIdentityProvider(...), ...)`.
    Both must pass identical assertions (for the 3 base-delegated methods).

**Construction is hermetic** (echoing P8b lesson): the constructor reads `model.conf` and
`policy.csv` from disk — this IS IO. Document explicitly that P8c construction does ONE small IO
(loading two text files into memory) and that's acceptable because (a) it's not network, (b) it's
local-filesystem only, (c) failure surfaces immediately. NO subprocess, NO network. Add this to
the class docstring.

---

## 7. File Layout

```
claw_engine/adapters/identity/
├── __init__.py                  # empty (package stub)
└── casbin/
    ├── __init__.py              # exports CasbinIdentityProvider, CasbinIdentityProviderError
    ├── provider.py              # CasbinIdentityProvider class + reload/add/remove methods
    ├── errors.py                # CasbinIdentityProviderError (adapter-local)
    ├── model.conf               # shipped PERM model (§2)
    └── policy.csv               # shipped example policy (§3), with comment header

tests/adapters/
└── test_casbin_identity.py     # 13 invariant tests (§6)

tests/contract/
└── (optional) identity_contract.py — only if no shared contract exists yet
```

`provider.py` soft cap: 200 lines. The class is small. If it grows beyond, flag.

---

## 8. pyproject extras

```toml
[project.optional-dependencies]
otel = ["opentelemetry-api>=1.20", "opentelemetry-sdk>=1.20"]
skills-git = []  # system git
identity-casbin = ["casbin>=1.30"]
```

Casbin Python package on PyPI: `casbin` (Apache-2.0).

Test gating: `pytest.importorskip("casbin")` at the top of `test_casbin_identity.py`.

---

## 9. Definition of Done

- [ ] All 13 invariants in §6 pass (plus optional contract suite if added)
- [ ] `pytest -q` → 252 + new tests, all green
- [ ] `pytest tests/purity -q` → 2/2 (engine still clean)
- [ ] `git diff v1.0.0..HEAD -- claw_engine/engine/` → empty
- [ ] `grep -rn "from claw_engine.adapters" claw_engine/engine/` → empty
- [ ] `grep -rn "casbin" claw_engine/engine/` → empty
- [ ] **Acceptance-critical**: workspace access decisions delegate purely to base (invariant #5)
- [ ] **Acceptance-critical**: edited policy.csv without `reload_policy()` → behavior unchanged
      (invariant #7) — proves no implicit watcher
- [ ] **Acceptance-critical**: `add_policy/remove_policy` without disk write (invariants #6)
- [ ] `[identity-casbin]` extra exists in pyproject; default install has no casbin
- [ ] Two-round superpowers review (spec + code quality) both pass before merge

---

## 10. Out of Scope (defer to later)

- **Group / role inheritance** (`g = _, _` policy section) — V1 flat table only
- **Workspace-access via Casbin** — locked in parent plan; needs separate plan with explicit-allow
  default-deny model
- **`resolve_user` via SSO/OIDC** — that's P8c+N (web/API channel only); IM channels don't need it
- **Auto-reload via file watcher** — locked; future adapter feature, separate plan
- **Disk write on add/remove** — explicit choice; if needed later, add a `save_policy()` method
- **gRPC / remote Casbin Service** — V1 embedded only

---

## 11. Hard Contracts (raised from "design choices" per user 2026-05-28)

User elevated three implementation choices to **hard contracts** — they are no longer "we'd
prefer this", they are "any violation blocks merge and requires a new plan".

### Hard Contract A — Construction IO surface (narrow)

`__init__` is permitted to read EXACTLY the two files passed in (`model_path` + `policy_path`),
loading them once into memory. No more, no less.

**Forbidden:**
- Network call of any kind
- Subprocess invocation
- Filesystem scan (no `Path.glob`, `os.walk`, `os.listdir`)
- Implicit fallback to a bundled `policy.csv` / `model.conf` when paths aren't provided
- File watcher registration (no `watchdog`, no `inotify`, no thread)
- Reading any path not explicitly provided by the caller

**Required tests:**
- `test_construction_does_not_fall_back_to_bundled_policy`: monkey-patch `Path.read_text` to
  bomb on any path not in `{model_path, policy_path}`; construction succeeds when both args
  are explicit; if either is omitted, raises `TypeError` (missing required arg) BEFORE any IO.
- `test_construction_does_no_subprocess`: monkey-patch `subprocess.run` to raise; constructor
  must complete without invoking it.

### Hard Contract B — Policy mutation never writes to disk

`add_policy` and `remove_policy` are **pure in-memory** operations on the Casbin enforcer.
`policy.csv` on disk is read once at construction (and on each `reload_policy()`) and is
otherwise treated as **read-only**.

**Required tests:**
- `test_add_policy_does_not_modify_policy_file`: snapshot `policy.csv` mtime and content
  bytes; call `add_policy(...)`; assert `policy.csv` mtime AND content bytes unchanged.
- `test_remove_policy_does_not_modify_policy_file`: same shape for `remove_policy`.
- `test_reload_policy_overrides_in_memory_mutations`: after `add_policy(...)`, calling
  `reload_policy()` reverts to the on-disk state (the added rule disappears unless it's on disk).

V1 does NOT implement `save_policy()`. If a caller needs to persist programmatic changes, they
either (a) edit `policy.csv` themselves and call `reload_policy()`, or (b) wait for a future
adapter version. Document explicitly in class docstring.

### Hard Contract C — Shipped `policy.csv` is example-only

The `policy.csv` shipped inside the adapter package (under `claw_engine/adapters/identity/casbin/`)
is **documentation / test fixture only**. It is NEVER loaded automatically.

**Constructor signature locks this:**
```python
def __init__(
    self,
    base: IdentityProvider,
    *,
    model_path: Path | str,   # required, no default
    policy_path: Path | str,  # required, no default
) -> None: ...
```

**Required tests:**
- `test_missing_model_path_raises`: `CasbinIdentityProvider(base)` (no model_path) → `TypeError`.
- `test_missing_policy_path_raises`: similar.
- `test_explicit_path_to_shipped_example_works`: caller CAN pass the shipped file's path
  explicitly if they really want to use it as a starting point — but they must do so explicitly.

---

## 11.1 Remaining Locked Decisions (no confirm round needed)

These are not "hard contracts" per the user's framing but are still locked:

1. **Wrapping pattern, not subclassing** — adapter takes a `base: IdentityProvider`. Allows any
   future provider to compose with Casbin.
2. **Casbin enforcer constructed once** in `__init__`, cached on `self._enforcer`. Reload mutates
   in place.
3. **Default-allow + deny-only matcher** (§2) — mirrors V1 `denied_skills` semantics.
4. **Flat policy table** — no group inheritance in V1.
5. **Adapter-local error type**: `CasbinIdentityProviderError` for malformed policy / missing
   files. Does NOT impersonate engine exceptions.
6. **`reload_policy` / `add_policy` / `remove_policy` are NOT in the engine Protocol** — they're
   adapter-specific control surface. Callers that need them downcast or use the adapter type
   directly.

---

## 12. Implementer Notes (per user 2026-05-28)

In addition to Hard Contracts A/B/C above:

### Note 1 — `can_access_workspace` is **100% delegated** to base, no exceptions

The adapter's `can_access_workspace(user, ws)` must `return self._base.can_access_workspace(user, ws)`
verbatim. Casbin enforcer is never queried for this method. Required regression: write a Casbin
policy that contains a wildcard `p, *, ws-X, *, *, deny` (which would block all skill/workflow
actions in workspace `ws-X`) AND let `base.can_access_workspace(alice, ws-X)` return `True`.
`adapter.can_access_workspace(alice, ws-X)` MUST return `True` (base wins, Casbin ignored for
workspace access).

### Note 2 — Default-allow semantics: test all three wildcard dimensions

`can_use_skill / can_run_workflow` returns True unless a Casbin deny rule matches.
Test wildcards on **three independent dimensions**:

- **User wildcard**: `p, *, ws, skill, use, deny` → `can_use_skill(alice, ws, skill) is False`
  AND `can_use_skill(bob, ws, skill) is False`
- **Workspace wildcard**: `p, alice, *, skill, use, deny` → blocked across all workspaces
- **Object wildcard**: `p, alice, ws, *, use, deny` → all skills blocked in ws for alice

Also test that wildcards on `act` work: `p, alice, ws, dangerous, *, deny` blocks both
`can_use_skill(alice, ws, dangerous)` AND `can_run_workflow(alice, ws, dangerous)`.

### Note 3 — Casbin dependency stays in adapter

- `import casbin` MUST only appear under `claw_engine/adapters/identity/casbin/`
- `claw_engine/engine/` must have ZERO `casbin` references
- `pyproject.toml` `[project.dependencies]` must NOT contain `casbin` — it lives under
  `[project.optional-dependencies].identity-casbin` only
- Default `pip install -e .` works without Casbin (verified by purity test + manual smoke)

---

## 13. Acceptance Focus (user 2026-05-28)

> Casbin handles skill/workflow deny decisions only; workspace access stays with the base;
> no implicit file watcher; engine Protocol unchanged; policy mutation does not touch disk;
> default install does not pull in Casbin.

**Verification on PR review will focus specifically on:**
1. Is `can_access_workspace` completely untouched by Casbin? (Note 1)
2. Does `add_policy` / `remove_policy` keep `policy.csv` byte-identical? (Hard Contract B)
3. Does editing `policy.csv` without `reload_policy()` change adapter behavior? (Must be NO.) (§6 #7)

Any deviation from these three checks requires a new plan, not a fix-in-place.
