"""
BB7 operator control CLI — `python -m sml_beast.outreach.opctl <command>`

The everyday operator surface. Wraps the state machine + guardrails so
common tasks don't require dropping into a Python REPL.

Commands:
  status                              — full state snapshot (counts, ledger, kill switch)
  opt-out <domain>                    — permanently blocklist a domain
  review-clear <vertical>             — record one manual review completed
  review-clear-all <vertical>         — clear the entire gate for a vertical (5x)
  kill on                             — activate kill switch (drops kill file)
  kill off                            — deactivate kill switch (removes kill file)
  metrics                             — dump conversion_metrics.jsonl as JSON
  metrics-stats                       — dump aggregate conversion stats
  domain <name>                       — inspect a single domain's lifecycle entry
  recent [N]                          — last N state changes (default 20)
  replies                             — dump operator review queue (ACCEPT/MANUAL_REVIEW)
  poll                                — one-shot IMAP poll; classify + persist
  dry-run                             — run one full cycle with NO XRPL / NO SMTP

Every mutating command prints a confirmation line before exiting.
None of these commands send a pitch. None submit XRPL. None send SMTP.
The dry-run command runs the full agent pipeline with both disabled.

Exit codes:
  0 = success
  1 = command-level failure (bad domain, unknown command)
  2 = system halt / kill switch / hard guardrail trip
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

logger = logging.getLogger("sml-beast.outreach.opctl")


# ── output helpers ────────────────────────────────────────────────────────────


def _ok(msg: str) -> None:
    print(f"[OK]   {msg}")


def _info(msg: str) -> None:
    print(f"[INFO] {msg}")


def _err(msg: str) -> None:
    print(f"[ERR]  {msg}", file=sys.stderr)


# ── command implementations ──────────────────────────────────────────────────


def cmd_status(args: argparse.Namespace) -> int:
    from .guardrails import OutreachGuardrails, kill_switch_path
    from .state import OutreachStateMachine
    from .verifier import conversion_stats

    sm = OutreachStateMachine()
    snap = sm.snapshot()
    counts = sm.domain_count_by_state()
    ledger = OutreachGuardrails.daily_ledger_snapshot()
    stats = conversion_stats()

    out = {
        "kill_switch_active": kill_switch_path().exists(),
        "warmup_mode": sm.is_in_warmup_period(),
        "days_since_first_pitch": sm.days_since_first_pitch(),
        "domain_counts_by_state": counts,
        "manual_review_completed": snap.get("manual_review_count_by_vertical", {}),
        "ledger": ledger,
        "conversion": stats,
    }
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def cmd_opt_out(args: argparse.Namespace) -> int:
    from .state import OutreachStateMachine

    domain = args.domain.strip().lower()
    if not domain or "." not in domain:
        _err(f"invalid domain: {args.domain!r}")
        return 1

    sm = OutreachStateMachine()
    sm.mark_opted_out(domain)
    _ok(f"{domain} added to permanent opt-out blocklist")
    return 0


def cmd_review_clear(args: argparse.Namespace) -> int:
    from .state import OutreachStateMachine

    vertical = args.vertical.strip().lower()
    if vertical not in ("mastersheets", "xrpl_x402"):
        _err(f"unknown vertical: {vertical!r}")
        return 1

    sm = OutreachStateMachine()
    sm.record_manual_review_completed(vertical)
    snap = sm.snapshot()
    count = snap["manual_review_count_by_vertical"].get(vertical, 0)
    _ok(f"{vertical} review counter += 1 (now {count}/5)")
    return 0


def cmd_review_clear_all(args: argparse.Namespace) -> int:
    from .state import OUTREACH_MANUAL_REVIEW_N, OutreachStateMachine

    vertical = args.vertical.strip().lower()
    if vertical not in ("mastersheets", "xrpl_x402"):
        _err(f"unknown vertical: {vertical!r}")
        return 1

    sm = OutreachStateMachine()
    snap = sm.snapshot()
    already = snap["manual_review_count_by_vertical"].get(vertical, 0)
    remaining = OUTREACH_MANUAL_REVIEW_N - already
    if remaining <= 0:
        _info(f"{vertical} gate already cleared ({already}/{OUTREACH_MANUAL_REVIEW_N})")
        return 0
    for _ in range(remaining):
        sm.record_manual_review_completed(vertical)
    _ok(f"{vertical} review gate cleared ({already + remaining}/{OUTREACH_MANUAL_REVIEW_N})")
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    from .guardrails import kill_switch_path

    ks = kill_switch_path()
    if args.state == "on":
        ks.parent.mkdir(parents=True, exist_ok=True)
        if ks.exists():
            _info(f"kill switch already active at {ks}")
        else:
            ks.write_text("halt activated via opctl\n")
            _ok(f"kill switch ACTIVATED — all new outreach is HALTED ({ks})")
        return 0
    elif args.state == "off":
        if not ks.exists():
            _info("kill switch already inactive")
            return 0
        ks.unlink()
        _ok("kill switch DEACTIVATED — outreach can dispatch again")
        return 0
    else:
        _err(f"kill state must be 'on' or 'off', got {args.state!r}")
        return 1


def cmd_metrics(args: argparse.Namespace) -> int:
    from .verifier import load_metrics

    records = load_metrics()
    print(json.dumps(records, indent=2))
    return 0


def cmd_metrics_stats(args: argparse.Namespace) -> int:
    from .verifier import conversion_stats

    stats = conversion_stats()
    print(json.dumps(stats, indent=2))
    return 0


def cmd_domain(args: argparse.Namespace) -> int:
    from .state import OutreachStateMachine

    sm = OutreachStateMachine()
    entry = sm.get_domain(args.name)
    if entry is None:
        _info(f"{args.name}: not tracked (never pitched / opted out)")
        return 0
    print(json.dumps(entry, indent=2, sort_keys=True))
    return 0


def cmd_recent(args: argparse.Namespace) -> int:
    from .state import OutreachStateMachine

    sm = OutreachStateMachine()
    snap = sm.snapshot()
    events = sorted(
        snap.get("domains", {}).items(),
        key=lambda kv: kv[1].get("last_state_change_ts_utc", 0),
        reverse=True,
    )[: args.n]
    out = []
    for domain, entry in events:
        out.append(
            {
                "domain": domain,
                "state": entry.get("state"),
                "vertical": entry.get("vertical"),
                "ts_utc": entry.get("last_state_change_ts_utc"),
                "tx_hash": entry.get("tx_hash"),
            }
        )
    print(json.dumps(out, indent=2))
    return 0


def cmd_balance(args: argparse.Namespace) -> int:
    """Query the hot wallet's XRPL balance and report USDC-equivalent."""
    from .balance import BalanceCheckError, check_hot_wallet

    try:
        result = check_hot_wallet()
    except BalanceCheckError as e:
        _err(f"balance check failed: {e}")
        return 2
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.healthy else 1


def cmd_alerts_sweep(args: argparse.Namespace) -> int:
    """Run a one-shot alert sweep — kill switch transition, review backlog."""
    from .alerts import check_and_alert

    results = check_and_alert()
    print(json.dumps([{"sent": r.sent, "reason": r.reason} for r in results], indent=2))
    return 0


def cmd_replies(args: argparse.Namespace) -> int:
    """Dump the operator review queue (ACCEPT + MANUAL_REVIEW + OPTOUT)."""
    from .reply_monitor import load_operator_queue

    records = load_operator_queue()
    print(json.dumps(records, indent=2))
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    """One-shot IMAP poll — drain UNSEEN, classify, persist. No mail sent."""
    from .reply_monitor import ReplyMonitorError, poll_inbox

    try:
        result = poll_inbox()
    except ReplyMonitorError as e:
        _err(f"reply monitor halted: {e}")
        return 2
    print(json.dumps(result.to_dict(), indent=2))
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    """Run one full cycle with XRPL + SMTP disabled. Useful for testing
    targeting + enrichment + template rendering without touching live systems."""
    os.environ["BB7_OUTREACH_DRY_RUN"] = "1"
    # Re-import so the agent module picks up the env var
    import importlib

    from . import agent

    importlib.reload(agent)
    summary = agent.run_cycle()
    print(json.dumps(summary, indent=2))
    return 0


# ── argparse wiring ──────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="opctl",
        description="BB7 outreach operator control surface",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("status", help="full state snapshot")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("opt-out", help="permanently blocklist a domain")
    s.add_argument("domain", help="domain to opt out (e.g., example.com)")
    s.set_defaults(fn=cmd_opt_out)

    s = sub.add_parser("review-clear", help="record one manual review completed")
    s.add_argument("vertical", help="mastersheets | xrpl_x402")
    s.set_defaults(fn=cmd_review_clear)

    s = sub.add_parser(
        "review-clear-all",
        help="clear the entire manual review gate for a vertical",
    )
    s.add_argument("vertical", help="mastersheets | xrpl_x402")
    s.set_defaults(fn=cmd_review_clear_all)

    s = sub.add_parser("kill", help="activate/deactivate kill switch")
    s.add_argument("state", choices=("on", "off"), help="'on' to halt, 'off' to resume")
    s.set_defaults(fn=cmd_kill)

    s = sub.add_parser("metrics", help="dump all conversion_metrics.jsonl records")
    s.set_defaults(fn=cmd_metrics)

    s = sub.add_parser("metrics-stats", help="dump aggregate conversion stats")
    s.set_defaults(fn=cmd_metrics_stats)

    s = sub.add_parser("domain", help="inspect a single domain's lifecycle")
    s.add_argument("name", help="domain to inspect")
    s.set_defaults(fn=cmd_domain)

    s = sub.add_parser("recent", help="last N state changes")
    s.add_argument("n", nargs="?", type=int, default=20, help="number of events (default 20)")
    s.set_defaults(fn=cmd_recent)

    s = sub.add_parser(
        "balance",
        help="query hot wallet XRPL balance (exit 0=healthy, 1=low, 2=error)",
    )
    s.set_defaults(fn=cmd_balance)

    s = sub.add_parser(
        "alerts-sweep",
        help="check kill switch / review backlog and emit any Discord alerts",
    )
    s.set_defaults(fn=cmd_alerts_sweep)

    s = sub.add_parser("replies", help="dump operator review queue (replies awaiting review)")
    s.set_defaults(fn=cmd_replies)

    s = sub.add_parser("poll", help="one-shot IMAP poll — drain UNSEEN, classify, persist")
    s.set_defaults(fn=cmd_poll)

    s = sub.add_parser(
        "dry-run",
        help="run one full agent cycle with XRPL+SMTP disabled (for testing)",
    )
    s.set_defaults(fn=cmd_dry_run)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except Exception as e:
        _err(f"unhandled exception: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
