# Design Write-up

## 1. Architecture

Three execution modes, kept structurally separate:

| Mode | Who decides | When | Output |
|---|---|---|---|
| Discovery | an LLM (observe→decide→act) | once per task, or re-learning | a saved, typed capability + evidence |
| Replay | no LLM — the capability drives | every production call | typed outputs / business outcome / failure |
| Escalation | a human, on the same live session | when either mode gets stuck | resumed run + a record of what happened |

Discovery is expensive and adaptive; replay is cheap, strict, and — the whole
point — never calls a model. Only an approved capability crosses from one to
the other.

The seam that makes this work is `SurfaceDriver`: `observe()`,
`resolve(SemanticLocator)`, and the basic actions. Both the agent loop and the
replay engine talk only to this interface, never to Playwright directly.
`WebDriver` is the one implementation I built. A desktop accessibility driver
would implement the same interface without the agent loop, replay engine, or
capability format changing, because capabilities record semantic targets
(role + accessible name), never raw CSS or coordinates.

Storage is files on disk, not a database — no multi-tenant/scaling concern is
in scope, and files are diffable and human-reviewable before being trusted.
Single synchronous process; a queue would sit between an agent-facing service
API and the replay engine if this needed to scale out, but that layer isn't
built.

## 2. Artifact schema

The capability (`src/cua/artifact/schema.py`) is a contract, not a step list:
typed inputs/outputs, a checkpoint, a policy/risk block, and per-step semantic
locators with a ranked fallback list.

**Semantic locators, not CSS.** The mock app is deliberately hostile (table
layout, no test IDs) to make this earn its keep. A locator tries role +
accessible name, then an anchored search, then CSS/XPath as a last resort.
Role+name survives markup changes that break a selector, and is the one signal
that also exists on a desktop accessibility tree (§4).

**`value_ref`/`extract_to`, never literal values.** A recorded step says
`value_ref: "input.member_id"`, not the literal string typed. This is what
makes a recording reusable instead of guessing which literals are parameters
by string-matching — and it keeps PII out of the capability file entirely;
only its shape and sensitivity classification are stored.

**`on_conditions` per step.** The error taxonomy lives in the contract itself:
a step can declare a recognized branch outcome (`"No such member"` → business
outcome `MEMBER_NOT_FOUND`), so replay reads a declaration instead of guessing
at runtime whether an unexpected state is a legitimate answer or a crash.

**Versioning.** `capability_id` + integer `version`; a re-recording bumps it
and old versions stay replayable. The schema has `policy.requires_approval`
and replay enforces it, but there's no service layer with an actual approve
endpoint — that's invoked via a CLI flag right now.

## 3. Determinism & error handling

Replay (`src/cua/replay/engine.py`) never calls a model — not just documented:
a test spawns a subprocess importing only `cua.replay.engine` and asserts
`google.genai` was never loaded, so the claim can't silently rot.

Determinism comes from four things: (1) locator resolution in priority order
with a uniqueness check — 0 or >1 matches is a detectable failure, never a
guessed pick, which matters concretely for a search results table with
multiple matches; (2) recorded `wait_for` post-conditions polled with a
timeout instead of fixed sleeps; (3) a checkpoint assertion before declaring
success — a click "working" is proven, not inferred from not throwing; (4) a
three-way result contract (`SUCCESS | BUSINESS_OUTCOME | FAILURE`) rather than
a boolean, so the single most common mistake this kind of system makes —
treating a legitimate answer as a crash — is harder to make by accident. I
verified this live: replaying the member-lookup capability with a nonexistent
member ID returns `BUSINESS_OUTCOME / MEMBER_NOT_FOUND` cleanly, with
evidence, not an exception.

On failure, the result carries `failed_step`/`expected`/`observed` plus an
`evidence_ref`, so debugging has the actual state the engine saw.

**Not built:** `recoverable` conditions are matched but have no actual
recovery routine wired up — a match currently just falls through to normal
failure if `wait_for` doesn't clear on its own. Auto-repair of a drifted
locator was deliberately not built at all: fail-closed-and-flag was chosen
over silent repair, since a wrong guess in core banking is worse than a
stopped request a human reviews.

## 4. Heterogeneity & multi-tenant

Not built — design only.

**Surface heterogeneity:** a `DesktopDriver` backed by the OS accessibility
tree would implement the same `SurfaceDriver` interface, and no capability
would need to change, since capabilities only reference role + accessible
name, which exist on both surfaces.

**Multi-tenant reuse:** represent a capability as a base version keyed by
something like `vendor_base: "vendor-x@2024"` plus a thin per-tenant override
layer (a renamed label, a different route prefix), rather than a full
recording per tenant. The signal for *when* a tenant needs an override,
instead of blind re-recording, would be a locator-health signal from
replay itself: if a base locator only resolves via its fallback tier for a
given tenant, that's the point to add an override. Concrete routes would
canonicalize into patterns (`/member/12345` → `/member/:id`) so one base
capability spans many instances of the same screen.

## 5. Escalation & handoff

A session has one explicit controller of record —
`Controller ∈ {AGENT, HUMAN, NONE}` — the single source of truth for who's
allowed to act. Three things trigger a handoff during discovery: the model
explicitly calling `escalate`, the model returning no usable response, or
three consecutive failed actions (a "looks stuck" signal separate from simply
running out of step budget).

The browser stays open on handoff — it runs headed specifically so a human can
act in the *same* live session — and the loop blocks on a terminal prompt. The
human acts directly in the visible window, presses Enter, `hand_back()` fires,
and the loop genuinely resumes from where it paused (recorded steps and
conversation history untouched, not a restart). Time waiting on the human
doesn't count against the run's timeout.

Stated plainly: "capture actions" records *that* a human intervened, why, and
the page state after — not a literal click-by-click trace, which would need
injecting JS event listeners into the page.

**Mocked deliberately:** the operator console is a terminal prompt, not a UI —
the brief allows this. What has to be real is the pause/resume mechanism and
controller state, and both are: I found and fixed a real bug during
development where escalation *raised an exception and killed the whole run*
instead of pausing it, which would have made this section dishonest if left
as-is.

## 6. Safety

Enforced at one choke-point (`safety/policy.check_action`) before every
action: permitted domains, route patterns, and action types from a config
file. An action outside it raises `PolicyViolation` and never reaches the
browser.

A capability declares `policy.risk_class` and `requires_approval`; replay
refuses to run an irreversible or approval-gated capability unless called
with `approved=True` — blocked by default.

**Redaction at capture time, not after.** Every typed/read value is tracked
and masked out of the persisted transcript before it's written, in both
discovery and replay — but the *return value to the calling agent* stays real,
since a balance-lookup capability has to actually return the balance. I found
this redaction was missing on the replay side while writing tests for it
(discovery had it, replay didn't), fixed it, and wrote a test that reads the
actual transcript file off disk and asserts the real values never appear.

**Stated limits:** redaction is field-aware, not content-aware — a screenshot
could still show a sensitive value on-screen. The allowlist is route-level,
not semantic.

## 7. Cuts

- **Operator console UI** → mocked as a terminal prompt; the pause/resume
  mechanism and controller state are real.
- **Desktop/native surface** → design only (§4).
- **Multi-tenant override loading and drift tooling** → design only (§4).
- **Recovery routines** for `recoverable` conditions → classified and matched,
  not auto-recovered.
- **Capability approval/registry service** → enforced by the schema and
  replay engine, invoked via CLI flags rather than an actual API + audit log.
- **Queues, a database, multi-process deployment** → single process, files.

Next, in order: the approval/registry API (the clearest gap for "an AI agent
invokes this in production" vs. a human running a CLI), then recovery
routines for the recoverable branch, then a second target workflow
(irreversible write + confirmation dialog) to exercise risk-class gating
end to end rather than only against a synthetic policy override.
