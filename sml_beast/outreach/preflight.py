"""
BB7 preflight validator — refuses to enable autonomous outreach until
every prerequisite checks out.

Run before the first mainnet cycle. The validator never sends a pitch,
never submits an XRPL transaction, and never mutates state. It only
inspects environment + network + secret material and reports red/yellow/green.

Checks performed:
  1. Required environment variables present and non-empty
  2. XRPL wallet seed parses to a valid Wallet object
  3. XRPL network reachable (RPC endpoint returns server_info)
  4. SMTP credentials authenticate (STARTTLS + LOGIN, no message sent)
  5. Outreach subdomain DNS records: A/AAAA, SPF, DMARC (DKIM is selector-specific
     and skipped here — operator verifies manually with their relay)
  6. Output directory writable; _internal/ subdirectory creates
  7. Kill switch file does NOT exist (warn if it does)
  8. Manual review counter status per vertical

Exit codes:
  0 = green   — safe to run live cycle
  1 = yellow  — soft warnings; operator decides whether to proceed
  2 = red     — hard failures; refuse to enable
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("sml-beast.outreach.preflight")


# ── result types ─────────────────────────────────────────────────────────────


SEVERITY_OK = "OK"
SEVERITY_WARN = "WARN"
SEVERITY_FAIL = "FAIL"


@dataclass
class CheckResult:
    name: str
    severity: str  # OK | WARN | FAIL
    detail: str = ""

    @property
    def is_fail(self) -> bool:
        return self.severity == SEVERITY_FAIL


@dataclass
class PreflightReport:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, severity: str, detail: str = "") -> None:
        self.results.append(CheckResult(name=name, severity=severity, detail=detail))

    def add_ok(self, name: str, detail: str = "") -> None:
        self.add(name, SEVERITY_OK, detail)

    def add_warn(self, name: str, detail: str = "") -> None:
        self.add(name, SEVERITY_WARN, detail)

    def add_fail(self, name: str, detail: str = "") -> None:
        self.add(name, SEVERITY_FAIL, detail)

    @property
    def has_failures(self) -> bool:
        return any(r.is_fail for r in self.results)

    @property
    def has_warnings(self) -> bool:
        return any(r.severity == SEVERITY_WARN for r in self.results)

    def exit_code(self) -> int:
        if self.has_failures:
            return 2
        if self.has_warnings:
            return 1
        return 0

    def to_dict(self) -> dict:
        return {
            "exit_code": self.exit_code(),
            "summary": {
                "ok": sum(1 for r in self.results if r.severity == SEVERITY_OK),
                "warn": sum(1 for r in self.results if r.severity == SEVERITY_WARN),
                "fail": sum(1 for r in self.results if r.severity == SEVERITY_FAIL),
            },
            "results": [
                {"name": r.name, "severity": r.severity, "detail": r.detail}
                for r in self.results
            ],
        }


# ── individual checks ────────────────────────────────────────────────────────


REQUIRED_ENV_VARS = (
    "BB7_XRPL_WALLET_SEED",
    "BB7_SMTP_HOST",
    "BB7_SMTP_USER",
    "BB7_SMTP_PASS",
    "BB7_OPT_OUT_URL",
    "BB7_OPERATOR_ADDRESS",
    "BB7_OPERATOR_SIGNATURE",
)

OPTIONAL_ENV_VARS = (
    "BB7_XRPL_NETWORK",
    "BB7_XRP_PRICE_USDC",
    "BB7_SMTP_FROM",
    "BB7_SMTP_PORT",
    "BEAST_OUTPUT_ROOT",
)


def check_env_vars(report: PreflightReport) -> None:
    """Verify all required env vars present and non-empty."""
    for var in REQUIRED_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if not value:
            report.add_fail(f"env:{var}", "required but not set or empty")
        else:
            # Don't echo secret values — just confirm presence
            length = len(value)
            report.add_ok(f"env:{var}", f"present (len={length})")

    for var in OPTIONAL_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            report.add_ok(f"env:{var}", f"present (len={len(value)})")


def check_xrpl_wallet(report: PreflightReport) -> None:
    """Verify the seed parses to a valid Wallet without exposing the seed."""
    seed = os.environ.get("BB7_XRPL_WALLET_SEED", "").strip()
    if not seed:
        return  # env check already flagged this

    try:
        from xrpl.wallet import Wallet
    except ImportError as e:
        report.add_fail("xrpl:import", f"xrpl-py not installed: {e}")
        return

    try:
        wallet = Wallet.from_seed(seed)
        addr = wallet.classic_address
        report.add_ok("xrpl:wallet", f"parsed; address={addr}")
    except Exception as e:
        report.add_fail("xrpl:wallet", f"seed invalid: {e}")


def check_xrpl_network(report: PreflightReport, *, fetch_fn=None) -> None:
    """Verify the configured RPC endpoint responds to server_info."""
    network = os.environ.get("BB7_XRPL_NETWORK", "testnet").strip()
    networks = {
        "testnet": "https://s.altnet.rippletest.net:51234",
        "mainnet": "https://xrplcluster.com",
    }
    rpc_url = os.environ.get("BB7_XRPL_RPC_URL", networks.get(network, ""))
    if not rpc_url:
        report.add_fail("xrpl:network", f"unknown network {network!r}")
        return

    if fetch_fn is not None:
        # Test injection path
        try:
            resp = fetch_fn(rpc_url)
            if resp:
                report.add_ok("xrpl:network", f"{network} reachable")
            else:
                report.add_fail("xrpl:network", f"{rpc_url} returned no data")
        except Exception as e:
            report.add_fail("xrpl:network", f"{rpc_url} unreachable: {e}")
        return

    try:
        import requests

        r = requests.post(
            rpc_url,
            json={"method": "server_info", "params": [{}]},
            timeout=10,
        )
        if r.status_code == 200 and r.json().get("result", {}).get("status") == "success":
            report.add_ok("xrpl:network", f"{network} server_info OK")
        else:
            report.add_fail("xrpl:network", f"{rpc_url} returned {r.status_code}")
    except Exception as e:
        report.add_fail("xrpl:network", f"{rpc_url} unreachable: {e}")


def check_smtp_auth(report: PreflightReport, *, smtp_factory=None) -> None:
    """Authenticate against the SMTP relay (LOGIN only — no message sent)."""
    host = os.environ.get("BB7_SMTP_HOST", "").strip()
    user = os.environ.get("BB7_SMTP_USER", "").strip()
    passwd = os.environ.get("BB7_SMTP_PASS", "").strip()
    if not (host and user and passwd):
        return  # already flagged by env check

    port = int(os.environ.get("BB7_SMTP_PORT", "587"))

    def _default_factory(h: str, p: int):
        smtp = smtplib.SMTP(h, p, timeout=15)
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        return smtp

    factory = smtp_factory or _default_factory
    try:
        smtp = factory(host, port)
        smtp.login(user, passwd)
        smtp.quit()
        report.add_ok("smtp:auth", f"{user}@{host}:{port} authenticated")
    except smtplib.SMTPAuthenticationError as e:
        report.add_fail("smtp:auth", f"auth rejected: {e}")
    except Exception as e:
        report.add_fail("smtp:auth", f"connection error: {e}")


def check_dns_records(report: PreflightReport, *, resolve_fn=None) -> None:
    """Verify A/SPF/DMARC for the outreach subdomain.

    DKIM is selector-specific and skipped (operator verifies with their relay)."""
    from_addr = os.environ.get(
        "BB7_SMTP_FROM", "outreach@infrastructure.scriptmasterlabs.com"
    ).strip()
    domain = from_addr.split("@")[-1] if "@" in from_addr else from_addr

    # A record
    try:
        if resolve_fn:
            addr = resolve_fn(domain, "A")
        else:
            addr = socket.gethostbyname(domain)
        report.add_ok("dns:A", f"{domain} → {addr}")
    except Exception as e:
        report.add_warn("dns:A", f"{domain} A lookup failed: {e}")

    # SPF / DMARC require TXT lookups — try dnspython, soft-warn if absent
    try:
        import dns.resolver
    except ImportError:
        report.add_warn(
            "dns:txt",
            "dnspython not installed; cannot verify SPF/DMARC (install with pip)",
        )
        return

    # SPF
    try:
        answers = dns.resolver.resolve(domain, "TXT")
        spf_found = any("v=spf1" in str(a).lower() for a in answers)
        if spf_found:
            report.add_ok("dns:spf", f"{domain} has SPF record")
        else:
            report.add_warn("dns:spf", f"{domain} missing SPF — mail will land in spam")
    except Exception as e:
        report.add_warn("dns:spf", f"SPF lookup failed: {e}")

    # DMARC
    dmarc_name = f"_dmarc.{domain}"
    try:
        answers = dns.resolver.resolve(dmarc_name, "TXT")
        dmarc_found = any("v=dmarc1" in str(a).lower() for a in answers)
        if dmarc_found:
            report.add_ok("dns:dmarc", f"{dmarc_name} has DMARC record")
        else:
            report.add_warn("dns:dmarc", f"{dmarc_name} missing DMARC")
    except Exception as e:
        report.add_warn("dns:dmarc", f"DMARC lookup failed: {e}")


def check_output_writable(report: PreflightReport) -> None:
    """Verify output root + _internal/ subdirectory writable."""
    root_str = os.environ.get("BEAST_OUTPUT_ROOT", "./output").strip()
    root = Path(root_str)
    internal = root / "_internal"
    try:
        internal.mkdir(parents=True, exist_ok=True)
        probe = internal / ".preflight_probe"
        probe.write_text("ok")
        probe.unlink()
        report.add_ok("fs:output", f"{internal} writable")
    except Exception as e:
        report.add_fail("fs:output", f"{internal} not writable: {e}")


def check_kill_switch(report: PreflightReport) -> None:
    """Warn loudly if the kill switch file exists — nothing will dispatch."""
    try:
        from .guardrails import kill_switch_path

        ks = kill_switch_path()
        if ks.exists():
            report.add_warn(
                "kill_switch",
                f"{ks} EXISTS — outreach is HALTED. Remove to enable dispatch.",
            )
        else:
            report.add_ok("kill_switch", "inactive")
    except Exception as e:
        report.add_fail("kill_switch", f"check failed: {e}")


def check_manual_review_state(report: PreflightReport) -> None:
    """Report which verticals still have an active manual review gate."""
    try:
        from .state import OUTREACH_MANUAL_REVIEW_N, OutreachStateMachine

        sm = OutreachStateMachine()
        snap = sm.snapshot()
        counts = snap.get("manual_review_count_by_vertical", {})
        for vertical in ("mastersheets", "xrpl_x402"):
            n = counts.get(vertical, 0)
            if n < OUTREACH_MANUAL_REVIEW_N:
                report.add_warn(
                    f"review_gate:{vertical}",
                    f"{n}/{OUTREACH_MANUAL_REVIEW_N} reviews completed — gate ACTIVE",
                )
            else:
                report.add_ok(
                    f"review_gate:{vertical}",
                    f"{n}/{OUTREACH_MANUAL_REVIEW_N} cleared — autonomous dispatch enabled",
                )
    except Exception as e:
        report.add_fail("review_gate", f"check failed: {e}")


# ── orchestrator ─────────────────────────────────────────────────────────────


def run_preflight(skip_network: bool = False) -> PreflightReport:
    """Run all preflight checks and return a single PreflightReport."""
    report = PreflightReport()

    check_env_vars(report)
    check_output_writable(report)
    check_kill_switch(report)
    check_manual_review_state(report)
    check_xrpl_wallet(report)

    if not skip_network:
        check_xrpl_network(report)
        check_smtp_auth(report)
        check_dns_records(report)
    else:
        report.add_warn("network_checks", "skipped (--skip-network)")

    return report


# ── CLI ──────────────────────────────────────────────────────────────────────


def _render_text(report: PreflightReport) -> str:
    """Render a report as a human-readable text block."""
    lines = []
    severity_glyph = {SEVERITY_OK: "[OK]  ", SEVERITY_WARN: "[WARN]", SEVERITY_FAIL: "[FAIL]"}
    for r in report.results:
        lines.append(f"{severity_glyph[r.severity]} {r.name:30s} {r.detail}")
    lines.append("")
    summary = report.to_dict()["summary"]
    lines.append(
        f"Summary: ok={summary['ok']} warn={summary['warn']} fail={summary['fail']} "
        f"→ exit={report.exit_code()}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BB7 outreach preflight validator")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument(
        "--skip-network",
        action="store_true",
        help="skip XRPL RPC + SMTP + DNS checks (for offline runs)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    report = run_preflight(skip_network=args.skip_network)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(_render_text(report))

    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
