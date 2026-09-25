# Project Plan — Computer-Use Automation System

Working plan + build tracker. Open this in VS Code alongside the code; we tick
boxes as we go. Ordering follows the vertical-slice rule: touch **every** core
requirement thinly, then deepen the three load-bearing pieces (schema · replay+errors
· safety/escalation).

**Status key:** ⬜ todo · 🟨 in progress · ✅ done · ✂️ intentionally cut (documented)

---

## Milestone 0 — Scaffold  ✅ (this skeleton)
- ✅ Repo layout + package structure
- ✅ Typed artifact schema (`src/cua/artifact/schema.py`) — focal point, written
- ✅ Replay result contract (`src/cua/replay/result.py`) — written
- ✅ Escalation controller state machine (`src/cua/escalation/controller.py`) — written
- ✅ Allowlist config + mock legacy target skeleton
- **Acceptance:** typed core imports + validates (verified).

## Milestone 1 — Real discovery run  🟨  *(the heart — must be real)*
- ✅ `surface/web.py`: Playwright `observe()` via `aria_snapshot()`; `resolve()` with
      priority order (role+name → anchored → fallbacks) + uniqueness check —
      verified live against the mock app (search, type, click, read, fail-closed).
- ✅ `agent/loop.py`: observe→decide→act with LLM tool-calling (Gemini free tier);
      record each step. Every click/type/navigate carries a model-stated `expect`
      that is re-verified before the step is accepted (self-correcting).
- ✅ Pre-action safety check (allowlist + risk) before every action — reuses
      Milestone 4's `safety/policy.py` (built ahead of schedule since Milestone 1
      needs it to gate the loop).
- ✅ Emit `Capability` + evidence into `/evidence/discovery-<ts>/`
- 🟨 **Acceptance:** the full action-execution path was verified by hand-driving the
      exact sequence a model would choose through the real `_execute` logic against
      the real mock app (produces a valid capability matching the hand-written
      example). The live LLM call itself is wired but not yet run end-to-end —
      blocked on a Gemini API key.

## Milestone 2 — Deterministic replay  ✅
- ✅ `replay/engine.py`: no-LLM execution; resolve semantic locators; verify checkpoint
- ✅ Typed input validation; return declared outputs
- ✅ **Acceptance:** replay of the saved artifact returns `SUCCESS` + `savings_balance`,
      no LLM calls, repeatable. Verified via `tests/test_replay.py` (6/6 passing,
      real mock app, real Playwright browser) — see also Milestone 3, done early.

## Milestone 3 — Error taxonomy  🟨  *(avoid the #1 mistake)*
- ✅ Evaluate `on_conditions` per step → business / hard (built into the replay
      engine alongside Milestone 2, since `ReplayResult`'s three-way status is
      load-bearing for the engine's own correctness)
- ⬜ Recovery routines (dismiss interstitial, wait/retry, single re-auth) —
      `recoverable` conditions are matched but not yet auto-recovered
- ✅ Failure path returns `failed_step / expected / observed`
- ✅ **Acceptance:** replay with `member_id=00000` returns
      `BUSINESS_OUTCOME / MEMBER_NOT_FOUND` cleanly (the required error replay) —
      verified live; `test_replay_business_outcome_is_not_a_failure`.

## Milestone 4 — Safety  ⬜
- ⬜ `safety/policy.py`: load allowlist; `check_action`; deny → block/escalate
- ⬜ Risk classes; irreversible actions gated (block unless approved)
- ⬜ Redaction at capture time (values never persisted raw)
- **Acceptance:** an off-allowlist route is blocked; sensitive fields absent from logs.

## Milestone 5 — Escalation & handoff  ⬜  *(real, not a TODO)*
- ⬜ Detect stuck (max steps / disallowed / risky / hard-fail)
- ⬜ `InterventionRequest` with context; `guard()` at the loop choke-point
- ⬜ Headed session stays alive; human acts in the SAME session; capture actions
- ⬜ Hand-back signal (CLI/endpoint — operator UI mocked) → resume from paused step
- **Acceptance:** trigger an intervention, act as the human, hand back, run resumes;
      handoff recorded in evidence.

## Milestone 6 — Write-up + evidence + design-only  ⬜
- ⬜ `REPORT.md` — 7 required headings, 1–3 pages
- ⬜ `README.md` — setup + exact demo commands (agent, then replay)
- ⬜ Design-only §4: surface abstraction + multi-tenant reuse & drift
- ⬜ `Cuts` section — what was stubbed/mocked and why + next steps
- **Acceptance:** a reviewer can set up, run the demo path, and follow the reasoning.

---

## Deliberate cuts (state in REPORT §7)
- ✂️ Real-time operator console → mocked (headed session + resume signal); mechanism real
- ✂️ Desktop/native surface → design only via `SurfaceDriver`
- ✂️ Multi-tenant override loading / drift tooling → design only
- ✂️ Queues / services / DB → single process, files
- Stretch (pick ≤1 if core solid): **agent-facing capability catalog** is highest value.

## The through-line to keep re-reading
goal → real LLM run → typed artifact → deterministic replay (outputs **and** one
error outcome) → real human takeover of the live session → evidence for both.
