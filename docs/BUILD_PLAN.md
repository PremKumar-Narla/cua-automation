# Design Notes

Working notes on the architecture and the reasoning behind the major decisions.
This is the source material for `REPORT.md`; kept separately because it's more
verbose than the final write-up needs to be.

---

## 0. The mental model

Three execution modes, kept structurally separate rather than blurred together:

| Mode | Who decides | When it runs | Output |
|---|---|---|---|
| **Discovery** | LLM (observe→decide→act) | first time, or when re-learning a task | a saved **capability** + evidence |
| **Replay** | no LLM — the capability drives | every production call | typed outputs / business outcome / failure |
| **Escalation** | a human, on the *same live session* | when either mode gets stuck | resumed run + record of what the human did |

Discovery is expensive and adaptive; replay is cheap and strict. The only thing
that crosses between them is an approved capability.

---

## 1. Stack decisions

| Decision | Pick | Why |
|---|---|---|
| Language | Python 3.11+ | Pydantic gives typed, serializable, versioned capability specs largely for free. |
| Browser automation | Playwright | Real UI driving with auto-waiting, and it exposes an accessibility snapshot — the bridge to targeting elements semantically instead of by pixel/CSS. |
| Perception fed to the LLM | Accessibility snapshot (role + name), screenshot as evidence only | The a11y tree is more stable than raw markup across cosmetic UI changes, and the same signal exists on desktop apps — it's the basis for the surface being swappable later. |
| LLM | Gemini (free tier), tool-calling | Structured tool-calling keeps the observe→decide→act loop clean and every recorded action typed. Provider is swappable behind one interface; picked for zero-cost iteration, not a technical requirement. |
| Capability storage | YAML/JSON files on disk, versioned | No DB/queue infrastructure needed for a single-process system; files are diffable and human-reviewable. |
| Architecture | Single process, synchronous | Simpler and sufficient at this scale; a queue would sit between the service API and the replay engine if this needed to scale out — not built. |

---

## 2. The core architectural seam: SurfaceDriver

```
        Goal + target
             |
     +---------------+        +----------------------------+
     |  Agent Loop    |--uses->|  SurfaceDriver (interface)  |
     | (LLM: obs/act) |        |  observe() -> Observation   |
     +-------+--------+        |  resolve(SemanticLocator)   |
             | records         |  click/type/navigate/read   |
     +-------v--------+        +--------------^--------------+
     |   Capability    |                       | implemented by
     +-------+--------+          +-------------+-------------+
             | drives            | WebDriver (Playwright)      | <- built
     +-------v--------+          | LegacyWebDriver              | <- design only
     |  Replay Engine  |---------+ DesktopDriver (a11y tree)     | <- design only
     +----------------+          +------------------------------+
```

Capabilities record **semantic targets** (role + accessible name + anchoring
context), not raw CSS selectors or coordinates. Both the agent loop and the
replay engine talk only to `SurfaceDriver` — swapping the surface never touches
the recorded flow. That's what makes "the same capability could in principle
drive a desktop accessibility tree instead of a browser" a real claim rather
than a hand-wave.

---

## 3. Capability schema — design rationale

The schema (`src/cua/artifact/schema.py`) is a contract, not a step list:
typed inputs, typed outputs, per-step semantic locators with a ranked fallback
list, recognized branch outcomes, a checkpoint, and a policy/risk block.

- **Why semantic locators, not CSS:** legacy bank UIs have no test IDs and
  non-semantic markup. Role + accessible name survives cosmetic changes that
  would break a CSS selector, and it's the one signal that also exists in a
  desktop accessibility tree.
- **Why `value_ref` / `extract_to` instead of literal values:** inputs are
  supplied at replay time and outputs are read at runtime. Baking a literal
  member ID or balance into the capability would break reuse across different
  inputs and would persist regulated data on disk. The capability stores
  *shapes* and *references*, never values.
- **Why `on_conditions` per step:** this is where the error taxonomy lives in
  the contract itself — the capability declares which exceptional states it
  recognizes and how to classify them, so replay reads a declaration instead
  of guessing at runtime.
- **Why `version` with immutable old versions:** a calling agent pins a
  version; a re-record bumps it, and old versions stay replayable.

---

## 4. Determinism & the error taxonomy

The single most common mistake this kind of system can make is treating a
legitimate business outcome ("no such member") the same as a crash. The result
contract (`src/cua/replay/result.py`) makes the three cases structurally
distinct:

- **`SUCCESS`** — typed outputs, goal reached.
- **`BUSINESS_OUTCOME`** — an expected, caller-relevant answer (e.g.
  `MEMBER_NOT_FOUND`). Not a failure; the caller decides what to do with it.
- **`FAILURE`** — a hard failure (target not found, checkpoint failed,
  timeout). Returns `failed_step` / `expected` / `observed` so it's debuggable.

Determinism comes from four things, all implemented in `replay/engine.py`:
1. Locator resolution in priority order (role+name → anchored → fallback),
   with a uniqueness check — 0 or >1 matches is a detectable failure, never a
   guessed pick.
2. Recorded `wait_for` post-conditions, polled with a timeout, instead of
   fixed sleeps.
3. A checkpoint assertion before declaring success.
4. No LLM in the decision path — the capability is the only decider, and this
   is enforced in code (a test asserts the replay engine never imports the LLM
   client), not just documented.

---

## 5. Safety model

- **Allowlist**, enforced at one choke-point before every action: permitted
  domains, route patterns, and action types. Anything outside it is blocked.
- **Risk classes**: read-only and reversible actions proceed; irreversible
  actions (submit/confirm/delete/transfer) are meant to require approval
  before unattended replay.
- **Redaction at capture time**: capabilities store input shapes and
  `value_ref`s, never literal PII; transcripts mask any value that was typed
  or read during a run before it's written to disk.
- **Stated limits**: redaction is field-aware, not content-aware — a
  screenshot could still show sensitive data on-screen. The allowlist is
  route-level, not semantic.

---

## 6. Escalation & handoff

Detect "stuck": step budget exhausted, a disallowed action, a risky action
needing a human decision, or a hard failure that can't be classified.

Control-transfer model: a session has one explicit controller of record
(`AGENT | HUMAN | NONE`). The automation loop calls `guard()` before every
action, which blocks while a human holds control. A stuck agent raises an
`InterventionRequest` carrying enough context to act (capability, goal,
current step, reason, state snapshot); a human resolves it and hands control
back, and the run resumes from the paused step on the same live session.

What's real vs. mocked: the pause/resume mechanism and controller state
machine are real. A full operator console UI is out of scope — the browser
runs headed so a human can act directly in the live session, with a minimal
signal to hand control back.

---

## 7. Target application

Built a small local "servicing console" (server-rendered, table layout, no
test IDs) rather than using a public site, because it makes the exceptional
states controllable: member-not-found, and (planned) permission-denied,
session-timeout, and a multi-match search result, so the error taxonomy can be
demonstrated against real conditions instead of only described. A localhost
app still counts as a real live surface for the discovery run requirement.

---

## 8. Design-only: heterogeneity & multi-tenant

Not built, but the abstractions are meant not to paint this into a corner:

- **Surface heterogeneity**: `SurfaceDriver` is the seam. A `DesktopDriver`
  backed by the OS accessibility tree would implement the same interface;
  capabilities wouldn't change since they only ever reference semantic
  targets.
- **Multi-tenant reuse**: represent a capability as a base version keyed by
  `vendor_base` (e.g. a specific vendor product + release) plus thin
  per-tenant overrides (a renamed label, a different route prefix). Detect
  drift by having replay emit a locator-health signal when a base locator only
  resolves via a fallback for a given tenant — that's the signal to add an
  override, rather than blindly re-recording per tenant.

---

## 9. Cuts

Declared deliberately, not accidental gaps:
- Real-time co-browsing operator console → mocked (headed session + a minimal
  resume signal); the underlying handoff mechanism is real.
- Desktop/native surface → design only, via `SurfaceDriver`.
- Multi-tenant override loading / drift tooling → design only.
- Queues/services/DB → single process, files.

---

## 10. Repo layout

```
/README.md          setup, how to run without live services, exact demo commands
/REPORT.md           architecture, schema, determinism, heterogeneity, escalation, safety, cuts
/evidence/            saved example capability + logs from a discovery run and replay runs,
                       including one replay that hits a business-outcome/exceptional state
/src/cua/
  agent/              observe->decide->act loop, LLM tool schema
  surface/            SurfaceDriver interface + WebDriver (Playwright)
  artifact/            capability schema, load/save, versioning
  replay/              deterministic engine + result contract
  safety/              allowlist, risk classes, redaction
  escalation/          controller state machine, intervention request
/config/              allowlist + target config (no secrets — .gitignore'd env)
/apps/mock_bank/      the local legacy-styled target
/tests/                replay determinism, error-taxonomy classification, drift handling
```
