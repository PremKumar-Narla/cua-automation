# Design Write-up

## 1. Architecture

There are three execution modes here, kept structurally separate:

| Mode | Who decides | When | Output |
|---|---|---|---|
| Discovery | an LLM (observe→decide→act) | once per task, or re-learning | a saved, typed capability + evidence |
| Replay | no LLM, the capability drives | every production call | typed outputs / business outcome / failure |
| Escalation | a human, on the same live session | when either mode gets stuck | resumed run + a record of what happened |

Discovery is expensive and adaptive. Replay is cheap and strict, and never
calls a model, which is the whole point of this design. Only an approved
capability crosses from one to the other.

The seam that makes this work is `SurfaceDriver`: `observe()`,
`resolve(SemanticLocator)`, and a handful of basic actions. Both the agent
loop and the replay engine talk only to this interface, never to Playwright
directly. `WebDriver` is the one implementation I actually built. A desktop
accessibility driver could implement the same interface without touching the
agent loop, the replay engine, or the capability format at all, because
capabilities record semantic targets (role + accessible name) instead of raw
CSS or screen coordinates.

Storage is just files on disk, not a database. There's no multi-tenant or
scaling concern in scope for this build, and files are easy to diff and
review by hand before anyone trusts them. It's a single synchronous process
too. A queue would sit between an agent-facing service API and the replay
engine if this ever needed to scale out, but I didn't build that layer.

## 2. Artifact schema

The capability format (`src/cua/artifact/schema.py`) is meant to be a
contract, not just a step list. It has typed inputs and outputs, a
checkpoint, a policy/risk block, and per-step semantic locators with a
ranked list of fallbacks.

Locators are semantic first, CSS last. The mock app is deliberately hostile
(table layout, no test IDs) so this actually gets tested. A locator tries
role plus accessible name first, then an anchored search, then CSS or XPath
as a last resort. Role and name survive markup changes that would break a
plain selector, and it's the one signal that also exists on a desktop
accessibility tree, which matters for section 4.

For values, a recorded step says `value_ref: "input.member_id"` rather than
the literal string that got typed. That's what actually makes a recording
reusable instead of having to guess which literals are parameters by
string-matching them later. It also keeps PII out of the capability file
entirely since only the shape and sensitivity classification get stored, not
the value.

The error taxonomy lives in the contract itself through `on_conditions`. A
step can declare a recognized branch outcome, so if the page says "No such
member," that maps to a business outcome called `MEMBER_NOT_FOUND`. Replay
reads that declaration instead of guessing at runtime whether an unexpected
page state is a legitimate answer or an actual problem.

Each capability has a `capability_id` and an integer `version`. A
re-recording bumps the version and old versions stay replayable. The schema
also has `policy.requires_approval`, and replay enforces it, but there's no
real service layer behind it yet with an approve endpoint. Right now it's
just a CLI flag.

## 3. Determinism and error handling

Replay (`src/cua/replay/engine.py`) never calls a model. I didn't just want
to claim that, so there's a test that spawns a subprocess importing only
`cua.replay.engine` and checks that `google.genai` was never loaded. That way
the claim can't quietly become false later if someone adds a convenience
import.

A few things make replay deterministic. Locators resolve in priority order
with a uniqueness check, so zero matches or more than one match is a
detectable failure rather than a guessed pick. That matters concretely for a
search results table with multiple matches. Recorded `wait_for`
post-conditions get polled with a timeout instead of fixed sleeps. A
checkpoint gets asserted before success is declared, so a click "working" is
actually proven rather than just inferred from nothing throwing. And the
result contract is three-way (`SUCCESS`, `BUSINESS_OUTCOME`, `FAILURE`)
instead of a boolean, so the single most common mistake this kind of system
tends to make, treating a legitimate answer as a crash, is harder to make by
accident. I checked this live: replaying the member-lookup capability with a
nonexistent member ID returns `BUSINESS_OUTCOME / MEMBER_NOT_FOUND` cleanly
with evidence, not an exception.

On failure, the result carries `failed_step`, `expected`, `observed`, and an
`evidence_ref`, so whoever's debugging gets the actual state the engine saw,
not just a message.

What's missing: `recoverable` conditions get matched but there's no actual
recovery routine behind them yet. A match currently just falls through to a
normal failure if `wait_for` never clears on its own. I also didn't build
any auto-repair for a drifted locator. That was a deliberate call, fail
closed and flag it rather than silently repair, because a wrong guess in
core banking is worse than a stopped request that a human looks at.

## 4. Heterogeneity and multi-tenant

Didn't build this part, design only.

A `DesktopDriver` backed by the OS accessibility tree would implement the
same `SurfaceDriver` interface, and no capability would need to change for
it, since capabilities only ever reference role and accessible name, and
both exist on desktop surfaces too.

For hundreds of tenants on the same underlying vendor product, I'd represent
a capability as a base version keyed by something like
`vendor_base: "vendor-x@2024"`, plus a thin per-tenant override layer for
things like a renamed label or a different route prefix, rather than a full
separate recording per tenant. The signal for when a tenant actually needs
an override, instead of blindly re-recording, would come from replay itself:
if a base locator only resolves through its fallback tier for a given
tenant, that's the point to add an override. Concrete routes would get
canonicalized into patterns too, so `/member/12345` becomes `/member/:id`,
letting one base capability span many instances of the same screen.

## 5. Escalation and handoff

A session has exactly one controller of record,
`Controller ∈ {AGENT, HUMAN, NONE}`, and that's the single source of truth
for who's allowed to act. Three things trigger a handoff during discovery:
the model explicitly calling `escalate`, the model returning no usable
response, or three consecutive failed actions in a row, which is a "looks
stuck" signal separate from just running out of step budget.

The browser stays open on handoff. It runs headed specifically so a human
can act in the same live session, and the loop blocks on a terminal prompt.
The human acts directly in the visible window, presses Enter, `hand_back()`
fires, and the loop genuinely resumes from where it paused. Recorded steps
and conversation history are untouched, it's not a restart. Time spent
waiting on the human doesn't count against the run's timeout either.

I'll be plain about the scope here: "capture actions" records that a human
intervened, why, and the page state afterward. It's not a literal
click-by-click trace, which would need injecting JS event listeners into the
page.

The operator console itself is mocked as a terminal prompt, not a UI, which
the brief allows. What has to be real is the pause and resume mechanism and
the controller state, and those are real. I actually found and fixed a bug
during development where escalation raised an exception and killed the whole
run instead of pausing it, which would have made this section dishonest if
I'd left it that way.

## 6. Safety

Everything gets checked at one choke-point, `safety/policy.check_action`,
before every action: permitted domains, route patterns, and action types,
all from a config file. An action outside that raises `PolicyViolation` and
never reaches the browser.

A capability declares `policy.risk_class` and `requires_approval`, and
replay refuses to run an irreversible or approval-gated capability unless
it's called with `approved=True`. Blocked by default.

Redaction happens at capture time, not after the fact. Every value that gets
typed or read is tracked and masked out of the persisted transcript before
it's written, in both discovery and replay. The return value to the calling
agent stays real though, since a balance-lookup capability has to actually
return the balance to be useful. I found this redaction was missing on the
replay side while writing tests for it (discovery already had it, replay
didn't), fixed it, and wrote a test that reads the actual transcript file
off disk and checks the real values never show up in it.

A couple of limits worth stating plainly: redaction is field-aware, not
content-aware, so a screenshot saved as evidence could still show a
sensitive value on screen. And the allowlist works at the route level, not
semantically, so it can't tell "read-only page" apart from "page that
happens to trigger a side effect" if one like that existed.

## 7. Cuts

These are intentional, not things I ran out of time for and forgot to
mention:

- Operator console UI, mocked as a terminal prompt. The pause/resume
  mechanism and controller state underneath it are real.
- Desktop or native surface, design only (section 4).
- Multi-tenant override loading and drift tooling, design only (section 4).
- Recovery routines for `recoverable` conditions. They're classified and
  matched, just not auto-recovered.
- A real capability approval and registry service. The schema and replay
  engine enforce it, but there's no actual API with an audit log yet, just
  CLI flags.
- Queues, a database, multi-process deployment. Single process, files on
  disk.

If I kept going, I'd build the approval and registry API first, since that's
the clearest gap between "a human runs a CLI" and "an AI agent invokes this
in production." After that, recovery routines for the recoverable branch,
then a second target workflow with an irreversible write and a confirmation
dialog, so risk-class gating gets exercised end to end instead of only
against a synthetic policy override in a test.
