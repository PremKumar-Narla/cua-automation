"""
Tests where they count (brief §7 code quality). Priorities to build together:
  * replay determinism: same inputs -> same steps -> same outputs
  * error taxonomy: bad member id -> BUSINESS_OUTCOME/MEMBER_NOT_FOUND (not a crash)
  * redaction: sensitive values never appear in persisted logs/artifacts
"""
from cua.replay.result import ReplayResult, ReplayStatus


def test_business_outcome_is_not_failure():
    r = ReplayResult.business("MEMBER_NOT_FOUND")
    assert r.status == ReplayStatus.BUSINESS_OUTCOME
    assert r.status != ReplayStatus.FAILURE
    assert r.outcome_code == "MEMBER_NOT_FOUND"
