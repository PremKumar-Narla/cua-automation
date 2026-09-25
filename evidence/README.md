# Evidence

The demonstration of the end-to-end flow (brief §6.3).

- `member.read_savings_balance.v1.json` — hand-written example capability, used as
  the fixture for `tests/test_replay.py` and to validate the schema/engine before
  discovery was live. `artifacts/` (gitignored) holds the same shape as generated
  live by `cua discover` and consumed by `cua replay`.
- `discovery-<ts>/` — transcript + screenshots from a real LLM discovery run
  (`cua discover`). Each line in `transcript.jsonl` is one observe/decide/act step.
- `replay-<ts>/` — transcript (+ screenshots) from a `cua replay` run. The outcome
  (`success` / `business_outcome` / `failure`) is in the transcript's last line;
  folders aren't pre-labeled by outcome since the engine doesn't know it in advance.
  At least one of each is kept here:
  - a **success** run (`member_id=12345` → `savings_balance`)
  - a **business outcome** run (`member_id=00000` → `MEMBER_NOT_FOUND`, not a crash)
  - a **fail-closed drift** run (a renamed target → clean `FAILURE` with the failing
    step, expected vs. observed, never a guess)
- (optional) a short screen recording
