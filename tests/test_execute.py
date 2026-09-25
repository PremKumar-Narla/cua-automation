"""
Tests for `_execute`, the action-handling core of the discovery loop.

Covers a real bug found via the first genuine live discovery run: a model
predicts `expect` (what should be true after an action) *before* it has seen
the result, so that prediction can be factually wrong even though the action
itself genuinely succeeded (e.g. it guessed the resulting heading would read
"Member Details" when the real page says "Member Detail"). The bug: a wrong
prediction was silently dropping the step from the recorded capability
entirely, producing an incomplete capability that then failed on replay --
even though the underlying browser action worked perfectly. Fixed so a step is
always recorded once its action succeeds; only the `wait_for` trusted for
replay depends on whether the prediction actually panned out.
"""
from __future__ import annotations

from cua.agent.loop import _execute


def _run(action, args, driver, **overrides):
    kwargs = dict(
        name=action, args=args, step_id="s1", driver=driver,
        allowlist={"permitted_domains": ["127.0.0.1"], "permitted_routes": ["/members/search"],
                   "permitted_actions": ["navigate", "click", "type", "read", "assert"]},
        base_url="", current_url="", inputs={"member_id": "12345"},
        outputs_declared={}, output_types={}, sensitive_values=[], redact_fields=set(),
        capability_id="test", goal="test",
    )
    kwargs.update(overrides)
    return _execute(**kwargs)


def test_type_is_recorded_even_when_the_predicted_expect_is_wrong(mock_bank_url, driver):
    driver.navigate(mock_bank_url + "/members/search")
    outcome = _run(
        "type",
        {"role": "textbox", "name": "Member ID", "value_ref": "input.member_id",
         "expect": {"role": "heading", "name": "Member Detail"}},  # wrong: typing alone never navigates
        driver, base_url=mock_bank_url, current_url=mock_bank_url + "/members/search",
    )
    assert outcome["status"] == "ok"
    assert outcome["step"] is not None
    assert outcome["step"].action.value == "type"
    # the prediction never landed, so it must not be trusted as a replay wait_for
    assert outcome["step"].wait_for is None
    assert "NOTE" in outcome["result_text"]


def test_click_records_confirmed_wait_for_when_the_prediction_is_correct(mock_bank_url, driver):
    url = mock_bank_url + "/members/search"
    driver.navigate(url)
    _run("type", {"role": "textbox", "name": "Member ID", "value_ref": "input.member_id",
                   "expect": {"role": "textbox", "name": "Member ID"}}, driver, base_url=mock_bank_url,
         current_url=url)
    outcome = _run(
        "click",
        {"role": "button", "name": "Search", "expect": {"role": "heading", "name": "Member Detail"}},
        driver, base_url=mock_bank_url, current_url=url,
    )
    assert outcome["status"] == "ok"
    assert outcome["step"].wait_for == {"role": "heading", "name": "Member Detail"}


def test_a_genuinely_unresolvable_target_is_still_an_error(mock_bank_url, driver):
    url = mock_bank_url + "/members/search"
    driver.navigate(url)
    outcome = _run(
        "click", {"role": "button", "name": "Does Not Exist", "expect": {"role": "heading", "name": "x"}},
        driver, base_url=mock_bank_url, current_url=url,
    )
    assert outcome["status"] == "error"
    assert outcome["step"] is None
