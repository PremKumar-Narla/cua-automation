# Computer-Use Automation System

An LLM works out how to complete a task inside a real UI that has no API; the
successful run is recorded as a typed, versioned **capability**; and that capability
**replays deterministically** afterward with no model in the loop — plus a human
takeover path and safety guardrails.

> Discovery (LLM decides) → Artifact (frozen capability) → Replay (no LLM) → Escalation (human, same live session)

## Status
- **Milestone 0** (scaffold): done.
- **Milestone 1** (real discovery run): built — Playwright driver, safety-checked
  agent loop, LLM tool surface — and verified end-to-end by hand-driving the exact
  action sequence through the real execution path against the real mock app. Only
  the live LLM call itself (which needs a free Gemini API key) is untested so far.
- **Milestone 2** (deterministic replay): done and tested — 6/6 tests passing
  against the real mock app (success, business-outcome-vs-crash, fail-closed on
  drift, determinism, input validation, zero-LLM-dependency check).
- Milestones 3–6: not started. See `PROJECT_PLAN.md` for the build order.

## Setup
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .                # installs the `cua` package so `python -m cua` resolves imports
playwright install chromium
cp .env.example .env            # add your Gemini API key (git-ignored)
```

The discovery step uses [Gemini's free tier](https://aistudio.google.com) (no credit
card required) — create a key there and put it in `.env` as `GEMINI_API_KEY`.

## Run the mock target
```bash
flask --app apps/mock_bank/app run     # serves the legacy-styled servicing console on :5000
```

## Demo path  *(the two commands a reviewer runs)*
```bash
# 1) Discovery: LLM drives the UI to complete a goal, then saves a capability artifact
python -m cua discover \
  --goal "Look up member 12345 and read their current savings balance" \
  --target config/target.servicing-console.yaml \
  --input member_id=12345

# 2) Replay: run the saved artifact deterministically, no LLM
python -m cua replay \
  --artifact artifacts/member.read_savings_balance.v1.json \
  --input member_id=12345

# 2b) Replay that hits an expected business outcome (the required error demo)
python -m cua replay \
  --artifact artifacts/member.read_savings_balance.v1.json \
  --input member_id=00000        # -> BUSINESS_OUTCOME / MEMBER_NOT_FOUND
```
Add `--headless` to either command to run without popping up a visible browser window.

## Running without live services
Replay and the error-taxonomy demo run against the local mock app — no external
services or real credentials, ever. Only the discovery run needs a (free) LLM API key.

## Layout
```
src/cua/artifact/    typed capability schema + persistence
src/cua/agent/       observe->decide->act loop + LLM tool surface (Gemini)
src/cua/surface/     SurfaceDriver seam + Playwright WebDriver
src/cua/replay/      deterministic engine + result contract (no LLM dependency)
src/cua/safety/      allowlist, risk classes, redaction
src/cua/escalation/  control-transfer state machine + intervention request
apps/mock_bank/      deliberately legacy-styled target
config/              allowlist + target config
evidence/            discovery + replay logs, screenshots (the demonstration)
artifacts/           saved capability specs (what discover writes, replay reads)
docs/                strategy + build notes
```

See `REPORT.md` for the design write-up and `docs/BUILD_PLAN.md` for detailed design notes.
