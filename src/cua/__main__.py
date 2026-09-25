"""
CLI entrypoint. `python -m cua discover ...` / `python -m cua replay ...`
"""
from __future__ import annotations

import argparse
import sys

import yaml

from cua.artifact.store import load as load_capability
from cua.replay.engine import replay
from cua.replay.result import ReplayStatus
from cua.safety.policy import load_allowlist
from cua.surface.web import WebDriver


def _parse_kv_pairs(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--input expects key=value, got: {pair!r}")
        key, value = pair.split("=", 1)
        out[key] = value
    return out


def _allowlist_path_for(app_id: str) -> str:
    return f"config/allowlist.{app_id}.yaml"


def cmd_discover(args: argparse.Namespace) -> int:
    from cua.agent.loop import DiscoveryEscalated, DiscoveryFailed, StopConditions, run_discovery

    target = yaml.safe_load(open(args.target))
    allowlist = load_allowlist(args.allowlist or _allowlist_path_for(target["app_id"]))
    inputs = _parse_kv_pairs(args.input)

    driver = WebDriver(headed=not args.headless)
    try:
        cap, run_dir = run_discovery(
            goal=args.goal,
            target=target,
            driver=driver,
            inputs=inputs,
            allowlist=allowlist,
            capability_id=args.capability_id,
            model=args.model,
            stop=StopConditions(max_steps=args.max_steps, timeout_s=args.timeout),
        )
    except DiscoveryEscalated as e:
        print(f"ESCALATED: {e.request.reason}", file=sys.stderr)
        print(f"see {run_dir if 'run_dir' in dir() else ''}", file=sys.stderr)
        return 2
    except DiscoveryFailed as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        driver.close()

    print(f"capability saved: {cap.capability_id} v{cap.version}")
    print(f"evidence: {run_dir}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    cap = load_capability(args.artifact)
    target = yaml.safe_load(open(args.target or f"config/target.{cap.target.app_id}.yaml"))
    inputs = _parse_kv_pairs(args.input)

    driver = WebDriver(headed=not args.headless)
    try:
        result = replay(cap, inputs, driver, base_url=target["base_url"])
    finally:
        driver.close()

    print(f"status: {result.status.value}")
    if result.status == ReplayStatus.SUCCESS:
        print(f"outputs: {result.outputs}")
    elif result.status == ReplayStatus.BUSINESS_OUTCOME:
        print(f"outcome_code: {result.outcome_code}")
    else:
        print(f"failed_step: {result.failed_step}")
        print(f"expected: {result.expected}")
        print(f"observed: {result.observed}")
    print(f"evidence: {result.evidence_ref}")
    return 0 if result.status != ReplayStatus.FAILURE else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cua")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="Run an LLM discovery session and save a capability")
    p_discover.add_argument("--goal", required=True)
    p_discover.add_argument("--target", required=True, help="path to a target config yaml")
    p_discover.add_argument("--allowlist", default=None, help="path to allowlist yaml (default: config/allowlist.<app_id>.yaml)")
    p_discover.add_argument("--input", action="append", default=[], help="key=value, repeatable")
    p_discover.add_argument("--capability-id", default=None)
    p_discover.add_argument("--model", default=None)
    p_discover.add_argument("--max-steps", type=int, default=25)
    p_discover.add_argument("--timeout", type=float, default=300.0)
    p_discover.add_argument("--headless", action="store_true")
    p_discover.set_defaults(func=cmd_discover)

    p_replay = sub.add_parser("replay", help="Replay a saved capability deterministically")
    p_replay.add_argument("--artifact", required=True)
    p_replay.add_argument("--target", default=None, help="path to a target config yaml (default: config/target.<app_id>.yaml)")
    p_replay.add_argument("--input", action="append", default=[], help="key=value, repeatable")
    p_replay.add_argument("--headless", action="store_true")
    p_replay.set_defaults(func=cmd_replay)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
