# Design Write-up

> 1–3 pages. Fill each section as the milestones land. Bullets below are the
> points to hit — replace with prose + your final decisions.

## 1. Architecture
- Three execution modes kept separate: discovery / replay / escalation.
- The `SurfaceDriver` seam: perception/action vs the recorded flow. Single process,
  files not DB (scaling infra is deliberately not built). Key trade-offs: TODO.

## 2. Artifact schema
- Contract not step-list: typed inputs/outputs, semantic locators, per-step branch
  outcomes, checkpoint, policy. Why semantic targets over CSS. Why value-refs (no
  literal PII). Versioning + provenance; transcript decoupled into /evidence.

## 3. Determinism & error handling
- Locator resolution priority + uniqueness check; recorded wait_for post-conditions;
  checkpoint assertion. Error taxonomy: business outcome vs recoverable vs hard
  failure, and how `on_conditions` drives classification. (Secondarily: UI drift.)

## 4. Heterogeneity & multi-tenant
- Surface abstraction → legacy web / desktop (a11y tree) via the same interface.
- Multi-tenant reuse: base capability keyed by `vendor_base` + thin per-tenant
  overrides; route canonicalization; locator-health drift signal instead of re-record.

## 5. Escalation & handoff
- Detect "stuck"; `controller ∈ {AGENT,HUMAN,NONE}` as single source of truth;
  pause/cede/resume on the SAME live session; capture human actions. Operator UI mocked.

## 6. Safety
- Allowlist enforced at one choke-point; reversible vs irreversible handling;
  redaction at capture time. Stated limits (route-level allowlist, screenshot leakage).

## 7. Cuts
- What was stubbed/mocked and why (operator console, desktop, multi-tenant plumbing,
  queues). What I'd build next with more time.
