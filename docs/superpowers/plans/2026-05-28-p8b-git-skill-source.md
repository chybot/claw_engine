# P8b: GitSkillSource Adapter — Sub-Plan

**Parent plan:** `/Users/lucas.xu/.claude/plans/cryptic-twirling-sonnet.md` § Tier-2 / P8b
**Baseline:** main @ `4a6a8c9` (after PR #1 P8a merge), tag `v1.0.0`
**Branch:** `feat/p8b-git-skill-source`

---

## Why this sub-plan exists

The parent plan correctly says **materialized source model**: constructor does no IO, `refresh()` is
the only IO point, `has_skill/has_dir/skill_dir` are hermetic. But "materialized" is loaded with
unspoken decisions: idempotency of `refresh()`, cache layout, error taxonomy, fixture shape,
concurrency semantics. Locking these down now prevents the implementer from making convenient-but-wrong
choices — especially the failure mode where `has_skill()` quietly falls back to a network call when
the cache is stale.

---

## 1. Lifecycle (Contract)

Three phases, **strict separation**:

| Phase | Methods | Allowed | Forbidden |
|---|---|---|---|
| **Construction** | `__init__(repo_url, ref, cache_dir, *, allowed_skill_paths=None)` | argument validation, path normalisation | **network, disk write, subprocess** — no exceptions |
| **Refresh** | `refresh() -> RefreshResult` | git subprocess, write to `cache_dir` | reading skill content |
| **Read** | `has_skill / has_dir / skill_dir / iter_skills (optional)` | read `cache_dir` only | network, disk write, subprocess |

**Construction is ALWAYS hermetic — no `eager` mode (corrected 2026-05-28)**: original draft had
an `eager=True` flag that ran `refresh()` from `__init__`. That violated the construction-phase
boundary above and would make adapter composition / DI roots implicit IO points (broken timeout,
credentials, error attribution, retry control). The flag has been removed. **Caller must call
`refresh()` explicitly** — no shortcuts. The acceptance test (§8 #1) monkey-patches `subprocess.run`
to fail; constructing `GitSkillSource(...)` with any kwargs must succeed without invoking it.

**Idempotency of `refresh()`**:
- Second call with **same `ref`** → checks out same commit; resolves to a no-op if cache already
  at that commit; else re-syncs sparse-checkout. Always safe to call again.
- Caller can rebind to a new ref via `refresh(ref=...)` (optional override param) — explicit ref
  change requires explicit call; the stored `self._ref` does NOT auto-mutate without `refresh()`.
- `refresh()` is NOT thread-safe — concurrent calls on the same `cache_dir` are caller's problem.
  Document; do not add locks in V1.

**Stale cache policy**: `has_skill / skill_dir` returning a result based on a stale ref is
**by design**. Caller must call `refresh()` when they want freshness. This is the same trade-off
as `LocalDirSkillSource` (which has no "refresh" concept at all).

---

## 2. Constructor

```python
class GitSkillSource:
    def __init__(
        self,
        repo_url: str,
        ref: str,
        cache_dir: str | Path,
        *,
        allowed_skill_paths: Sequence[str] | None = None,
    ) -> None:
        # validate inputs and store — NO IO, NO subprocess
```

| Param | Validation | Notes |
|---|---|---|
| `repo_url` | non-empty string; no further validation (git CLI rejects garbage) | supports `https://`, `git@`, `file://` (latter for fixtures) |
| `ref` | non-empty string | branch / tag / commit SHA |
| `cache_dir` | converted to `Path`; created on `refresh()` if missing | NOT validated to be empty / pre-existing; **NOT mkdir'd in `__init__`** |
| `allowed_skill_paths` | each entry passes path-traversal validation (see §5.1); skill-name basename also passes `validate_skill_name` (note: §5.1 validation may run in `__init__` since it is pure-string, no IO) | when `None` (default), `refresh()` does a full clone and `read` methods scan the repo root one-level deep, treating each top-level directory as a skill candidate. Pass an explicit list only when the repo is large or contains non-skill content. **Both code paths must be tested.** |

**No `eager` parameter** (removed 2026-05-28 — see §1). The constructor never touches `cache_dir`,
never invokes subprocess, never makes a network call. Caller calls `refresh()` explicitly when
they're ready to materialize.

---

## 3. `refresh()` Internals

```python
def refresh(self, *, ref: str | None = None) -> RefreshResult:
    ...
```

Algorithm:

1. Optional `ref` override → `self._ref = ref` (after validation: non-empty string).
2. **Validate every entry in `allowed_skill_paths`** via `_validate_allowed_path()` (§5.1) before any subprocess call. Reject early.
3. If `cache_dir` doesn't exist or is empty → `git clone --depth=1 --filter=blob:none --no-checkout <repo_url> <cache_dir>`.
4. Inside `cache_dir`:
   - `git sparse-checkout init --cone` (cone mode = fast, restricts to whole-directory patterns)
   - If `allowed_skill_paths`: `git sparse-checkout set <path1> <path2> ...`; else `git sparse-checkout disable` (full tree)
   - `git fetch --depth=1 origin <ref>` (in case ref isn't HEAD)
   - `git checkout <ref>` (works for branches, tags, commit SHAs after fetch)
5. Return `RefreshResult(ref=<resolved-commit-sha>, materialized_paths=[...])`.

`RefreshResult` dataclass (frozen) — **defined in `source.py` next to `GitSkillSource`**, not a separate `refresh.py`. It's an adapter-local return type, not an engine contract:
- `ref: str` — the commit SHA actually checked out
- `materialized_paths: tuple[str, ...]` — what's now in cache_dir (skill names, derived from `allowed_skill_paths` or directory scan)

**git subprocess wrapper**: single helper `_git(*args, cwd: Path) -> subprocess.CompletedProcess`
that runs `subprocess.run(["git", *args], cwd=cwd, check=False, capture_output=True, text=True)`
and on non-zero exit raises `GitSkillSourceError(stage: str, returncode: int, stderr: str)` with
stderr trimmed to 1000 chars.

**No `git pull`** — always `fetch + checkout` for determinism. Avoids merge weirdness when local
state diverges (it shouldn't, but be defensive).

---

## 4. Read Methods

All read methods are equivalent to running `LocalDirSkillSource(cache_dir)` on the materialized
directory. **Implementation approach**: compose, don't reimplement.

**Hard rule — acceptance gate**: `has_skill / has_dir / skill_dir` must **NEVER** invoke `git`
or any subprocess. The monkey-patch test below (§8 invariant 3) is non-negotiable evidence.

When `allowed_skill_paths=None`, the "candidate set" is derived from a one-level scan of
`cache_dir` after refresh — done once, cached on `self._local`, never re-scanned per call. No
subprocess.

```python
class GitSkillSource:
    def __init__(self, ...):
        self._local: LocalDirSkillSource | None = None  # set after refresh
    
    def refresh(self, *, ref=None):
        ...do git stuff...
        self._local = LocalDirSkillSource(self._cache_dir)
        return RefreshResult(...)
    
    def has_skill(self, name: str) -> bool:
        if self._local is None:
            return False  # not refreshed yet
        return self._local.has_skill(name)
    
    def has_dir(self, name: str) -> bool: ...   # delegate
    def skill_dir(self, name: str) -> str: ...  # delegate (raises SkillNotFound from LocalDirSkillSource)
```

**Pre-refresh behaviour**: `has_skill=False`, `has_dir=False`, `skill_dir()` raises
`SkillNotFound` (NOT `GitSkillSourceError` — pre-refresh isn't an *error*, the skill is just absent).

`validate_skill_name` is still enforced because `LocalDirSkillSource` calls it. Don't re-validate
in `GitSkillSource`.

---

## 5.1 Path Validation for `allowed_skill_paths` (Security Gate)

**Per-user requirement (2026-05-28)**: `allowed_skill_paths` is a new input surface. Every entry
must pass `_validate_allowed_path(p: str)` BEFORE entering any subprocess call. Reject:

| Reject if | Why |
|---|---|
| `os.path.isabs(p)` | absolute path → escapes `cache_dir` |
| `".." in p.split("/")` | traversal segment |
| `p.startswith("/")` or `p.startswith("\\")` | absolute marker |
| `"\\" in p` | backslash → Windows path / escape attempts |
| any segment is empty (`"foo//bar"`) | post-split has `""` segments, lets git resolve weird |
| `p` starts/ends with whitespace | trim ambiguity → git misinterprets |
| `len(p) > 256` | DoS / weird |
| segments fail this same check recursively | applies to every `/`-split piece |

After path validation, the **basename** (last `/`-split segment) is fed to
`engine/skills/source.py::validate_skill_name` to enforce the existing skill-name rules
(`[A-Za-z0-9_.-]+`, no `.` `..` etc.). This is two layers: path validation guards the input
surface; skill-name validation guards engine compatibility.

`_validate_allowed_path` raises `GitSkillSourceError(stage="validate", ...)` on failure
(adapter-local error; do not impersonate `InvalidSkillName` which is engine's).

Test cases (must all be in `tests/adapters/test_git_skill_source.py`):

- `["/etc/passwd"]` → reject
- `["../etc"]` → reject
- `["skills/../etc"]` → reject
- `["skills//double"]` → reject
- `["skills\\evil"]` → reject
- `["  skills/foo"]` → reject (leading whitespace)
- `["skills/foo"]` + `["skills/algo/dag_tracer"]` → accept (nested paths OK)
- `["foo"]` flat → accept

## 5.2 Error Matrix

| Failure mode | What happens | Exception | Stage label |
|---|---|---|---|
| `cache_dir` permission denied | `mkdir` fails | `GitSkillSourceError` | `"mkdir"` |
| Repo URL invalid / unreachable | `git clone` non-zero | `GitSkillSourceError` | `"clone"` |
| Auth required (SSH key missing / HTTPS prompt) | git exits non-zero with auth message | `GitSkillSourceError` | `"clone"` or `"fetch"` |
| `ref` doesn't exist | `git fetch` or `git checkout` non-zero | `GitSkillSourceError` | `"fetch"` or `"checkout"` |
| Network timeout | git exits non-zero | `GitSkillSourceError` | whatever stage hit it |
| Sparse-checkout pattern invalid | git exits non-zero | `GitSkillSourceError` | `"sparse-checkout"` |
| `allowed_skill_paths` contains invalid entry | per §5.1 | `GitSkillSourceError` | `"validate"` |
| Read pre-refresh | not an error | returns `False` / raises `SkillNotFound` | — |
| Read missing skill post-refresh | not an error | returns `False` / raises `SkillNotFound` | — |
| Read invalid name (path traversal) | `validate_skill_name` in `LocalDirSkillSource` | `InvalidSkillName` | — |

`GitSkillSourceError` is adapter-local; **does not impersonate engine exceptions**. Engine code
catches it as `Exception` and decides policy. Mirror P8d's `SecretProviderError` taxonomy choice.

`__cause__` chain: when wrapping a `subprocess.CalledProcessError`, set `raise GitSkillSourceError(...) from e` so debuggers see the underlying git error.

---

## 6. File Layout

```
claw_engine/adapters/skills/
├── __init__.py                  # empty (package stub)
└── git/
    ├── __init__.py              # exports GitSkillSource, GitSkillSourceError, RefreshResult
    ├── source.py                # GitSkillSource class, RefreshResult, _git helper, refresh() — all here
    └── errors.py                # GitSkillSourceError

tests/adapters/
└── test_git_skill_source.py    # invariant tests (lifecycle, security, path validation, error matrix)

tests/contract/
├── skillsource_contract.py     # NEW — shared SkillSource contract (LocalDir + Git both pass)
├── test_skillsource_contract.py
└── skillsource_fixtures.py     # NEW — bare-repo fixture builders
```

`source.py` holds everything except the error class: `GitSkillSource`, `RefreshResult`,
`_validate_allowed_path`, `_git()` subprocess helper, `refresh()` algorithm, read delegation.
Soft cap: ~250 lines (vs P8a's 202). If it grows beyond that during implementation, flag as a
concern — but don't preemptively split into `refresh.py` (per user decision: avoid over-splitting
small files).

---

## 7. Test Fixture: Local Bare Repo

**No testcontainers, no Gitea, no network.** Use git's own `--bare` to create a self-contained
repo on disk in `tmp_path`.

`tests/contract/skillsource_fixtures.py` provides:

```python
def make_bare_repo_with_skills(
    base: Path,
    *,
    skills: Mapping[str, str],  # skill_name → SKILL.md content
    layout: str = "flat",  # "flat" → skills at repo root; "nested" → under skills/<name>/SKILL.md
) -> str:
    """
    Build a local bare repo at base/repo.git with the given skills committed on `main`,
    return the repo URL (file:// path).
    """
    # 1. git init --bare base/repo.git
    # 2. git clone base/repo.git base/work
    # 3. for name, content in skills.items(): mkdir + write SKILL.md
    # 4. git add . && git commit && git push
    # 5. cleanup base/work, return file://base/repo.git
```

Why `file://` URL: `git clone file://...` exercises the same `git clone` code path as a real
remote, so the adapter doesn't get a special case for "local repos". `file://` URLs work with
sparse-checkout. **Do not use directory paths directly** (they trigger git's local-repo "hardlink
optimisation" which behaves differently from real remotes).

The fixture should also support:
- `make_bare_repo_with_skills(...)` → repo with valid skills
- `make_bare_repo_missing_skill_md(...)` → skill dir with no `SKILL.md` (tests `invalid` bucket)
- `make_bare_repo_empty(...)` → no skills committed (tests `missing` bucket)

---

## 8. Test Cases

### Contract suite (`tests/contract/test_skillsource_contract.py`)

Parametrised across `LocalDirSkillSource` and `GitSkillSource(refreshed)`. Both must pass:

1. `has_skill("foo")` returns `True` after `foo/SKILL.md` is present
2. `has_skill("missing")` returns `False`
3. `has_dir("foo")` returns `True`, `has_dir("missing")` returns `False`
4. `skill_dir("foo")` returns the path containing `SKILL.md`
5. `skill_dir("missing")` raises `SkillNotFound` (or whatever engine contract specifies)
6. Invalid name `"../evil"` raises `InvalidSkillName` for both
7. `has_skill` for a dir without `SKILL.md` returns `False`; `has_dir` returns `True`
   (the `invalid` bucket in Provisioner)

For `GitSkillSource` cases, fixture calls `refresh()` once before assertions; `LocalDir` cases
get the same content in a regular directory.

### Lifecycle invariants (`tests/adapters/test_git_skill_source.py`)

1. **No-IO construction (acceptance-critical)** ⭐: `GitSkillSource("file:///nonexistent.git", "main", tmp_path)`
   does NOT raise and does NOT invoke `subprocess.run`. Monkey-patch `subprocess.run` to bomb;
   construction must succeed with any combination of valid kwargs. Post-construction,
   `has_skill("x") is False` (no error). This invariant has no exceptions — no `eager` mode.
2. **Construction does not create `cache_dir`** ⭐: pass a non-existent `cache_dir` path; after
   construction, `cache_dir.exists()` is False. Only `refresh()` creates it.
3. **Pre-refresh hermetic** ⭐ **acceptance-critical**: `has_skill`, `has_dir`, `skill_dir`
   never invoke subprocess. Monkey-patch `subprocess.run` to raise on call; all three read
   methods must succeed (return `False` or raise `SkillNotFound`), proving they take no git path.
4. **Post-refresh hermetic** ⭐ **acceptance-critical**: same monkey-patch test AFTER `refresh()`
   has succeeded. Read methods still must not touch subprocess.
5. **Idempotent refresh**: calling `refresh()` twice in sequence yields the same `RefreshResult`
   (or one with the same `ref` SHA).
6. **Re-ref refresh**: build a bare repo with two commits on different branches; first
   `refresh(ref="main")` then `refresh(ref="branch2")` correctly switches the checkout.
7. **Allowed paths filter (explicit list)**: pass `allowed_skill_paths=["foo", "bar"]`; only
   those skills materialize.
8. **Allowed paths default (`None`)**: with `allowed_skill_paths=None`, refresh against a bare
   repo with `["alpha", "beta", "gamma"]` at root — all three become `has_skill=True` after
   refresh. Top-level scan only (nested dir without SKILL.md does NOT appear as a skill name).
9. **Nested layout**: `allowed_skill_paths=["skills/algo/dag_tracer"]` against a repo with
   `skills/algo/dag_tracer/SKILL.md` — `has_skill("dag_tracer")` is `True` (basename), and
   `skill_dir("dag_tracer")` returns the materialized full path.

### Error invariants

10. **Bad repo URL**: `refresh()` with `file:///does/not/exist.git` raises `GitSkillSourceError`
    with `stage="clone"`.
11. **Bad ref**: valid repo, `ref="nonexistent-branch"` → `GitSkillSourceError` with
    `stage in {"fetch", "checkout"}`.
12. **No `git` on PATH**: monkey-patch `subprocess.run` to raise `FileNotFoundError` → caught and
    wrapped in `GitSkillSourceError(stage="invoke", ...)`.

### Security invariants

13. **Path validation matrix** ⭐ **acceptance-critical**: every reject case from §5.1 (absolute
    path, `..`, empty segment, backslash, leading whitespace, oversize) → `GitSkillSourceError(stage="validate")`
    raised **before any subprocess call**. Verify by monkey-patching `subprocess.run` to fail —
    validation must catch the bad input before git is reached.
14. **cache_dir realpath containment**: after refresh, every file in `cache_dir` resolves
    (via `realpath`) under `cache_dir.realpath()`. No symlinks escape.

### Integration with Provisioner ⭐ **acceptance-critical**

15. Build a `SkillProvisioner` with `GitSkillSource` + `InMemoryIdentityProvider`; provision a
    workspace with `allowed_skills=["foo"]`. Verify:
    - `foo/SKILL.md` lands in the workspace `.agents/skills/foo/`
    - `denied_skills=["foo"]` (via Identity) → `foo` in `denied` bucket (NOT materialized)
    - After a second refresh that drops `foo` from the repo, re-running provisioner moves `foo`
      to the `revoked` bucket and **physically removes it from disk** (the V1 revocation invariant)
    - A skill dir without `SKILL.md` (built into the fixture) → `invalid` bucket; NOT materialized
    - Path-traversal in workspace `allowed_skills` (engine-side): still blocked by Provisioner's
      `validate_skill_name`, unaffected by the new adapter
   
   This test proves the adapter does NOT bypass any of Provisioner's existing safety semantics.

---

## 9. pyproject extras

```toml
[project.optional-dependencies]
otel = ["opentelemetry-api>=1.20", "opentelemetry-sdk>=1.20"]
skills-git = []  # no Python deps; uses system git CLI
```

Adapter requires **system `git`** on `PATH`. Document in `README.md` (P8b adds a one-paragraph
note). No Python git library (GitPython et al.) — keeps dep tree clean.

Test gating: tests use `shutil.which("git")` at module top in `pytest.importorskip`-equivalent
fashion (`pytest.skip("git not on PATH")` if unavailable). Don't add a `pytest.importorskip` for
the `skills-git` extra since it has no Python imports to skip on.

---

## 10. Out of Scope (defer to later)

- **Auto-refresh on stale cache** — caller's responsibility in V1
- **Concurrent-safe refresh** — single-threaded assumption; add `Lock` only when needed
- **Credentials management** (HTTPS tokens, SSH agent) — caller's git config; adapter passes
  through whatever `subprocess.run` inherits
- **Partial-clone optimization** beyond `--filter=blob:none` — current settings sufficient for
  skill repos
- **gitea/testcontainers tests** — bare-repo fixture is enough; testcontainers would be P8b+1
  or in `tests/integration/`

---

## 11. Definition of Done

- [ ] All 15 invariant tests + contract suite passing (16 with both flat-default + nested-explicit
      layout cases)
- [ ] Contract suite parametrised across LocalDir + Git; both pass identical assertions
- [ ] `tests/purity -q` 2/2 (engine still clean)
- [ ] Engine zero diff vs `v1.0.0`: `git diff v1.0.0..HEAD -- claw_engine/engine/` empty
- [ ] **Pre- AND post-refresh read methods do NOT invoke subprocess** (monkey-patch evidence)
- [ ] **Path validation rejects all §5.1 cases BEFORE subprocess** (monkey-patch evidence)
- [ ] **SkillProvisioner integration**: revoke / invalid / denied semantics unchanged by adapter
- [ ] PR description mirrors P8a format: spec compliance checklist, verification block, follow-ups
- [ ] `[skills-git]` extra in `pyproject.toml`; `README.md` mentions system `git` requirement
- [ ] Two-round superpowers review (spec + code quality) both pass before merge

---

## 12. Locked Decisions (2026-05-28)

User dispatched these decisions instead of waiting for a confirm round.

**Correction (2026-05-28, post-PR2 review):** original §1/§2 mentioned an `eager=True` mode that
ran `refresh()` from `__init__`. That contradicted the same section's "construction = no IO"
boundary. Removed entirely — see §1, §2, §8 #1, #2.

1. **`allowed_skill_paths=None` default kept** — semantics = "scan repo root one level for skill
   candidates"; explicit list = "use these exact paths (flat or nested)". **Both code paths
   tested** (invariants 7 + 8).

2. **`RefreshResult` lives in `source.py`** — adapter-local return type, no separate `refresh.py`.
   `errors.py` keeps `GitSkillSourceError`.

3. **No `iter_skills()` added to engine `SkillSource` Protocol** — engine contract does not grow
   in P8. "List candidates" need is adapter-internal; the candidate-set derivation runs once at
   `refresh()` and is cached on `self._local`.

4. **Two repo layouts supported**:
   - **Flat (default)**: `<repo>/<skill>/SKILL.md`. With `allowed_skill_paths=None`, scan one
     level deep at repo root and take each dir's basename as the skill name.
   - **Nested explicit**: `allowed_skill_paths=["skills/algo/dag_tracer"]`. Skill name =
     `os.path.basename(path)` = `"dag_tracer"`. Still runs `validate_skill_name(basename)`.

5. **NEW security requirement (per user 2026-05-28)**: §5.1 above. Every entry in
   `allowed_skill_paths` is now a tested input surface. Rejection happens **before** any
   subprocess call. Adapter-local `GitSkillSourceError(stage="validate")`, not engine's
   `InvalidSkillName`.

6. **Acceptance focus** (per user):
   - `has_skill / has_dir / skill_dir` must never trigger `git` or subprocess (invariants 3 + 4)
   - All git IO confined to `refresh()` (testable: every git call is reached only from `refresh`)
   - SkillProvisioner's revocation / invalid / denied semantics not bypassed by adapter (invariant 15)
