"""
BB7 outreach agent — autonomous pitch dispatch entrypoint.

Cron-friendly: runs one full cycle (load targets → enrich → reserve →
pay → send → record) and exits. State is fully restart-safe via
OutreachStateMachine. Each invocation picks up where the last left off.

Orchestration contract:
  1. Load bounty_targets.json for each configured vertical
  2. For each target (priority-ordered), run the dispatch loop:
       a. Enrich: get contact email via enricher.py
       b. Manual review gate: if needs_manual_review(), skip with a warning
          (operator must clear the gate before dispatch is allowed)
       c. Eligibility: atomic_reserve_pitch() — kill switch + blocklist +
          cooldown + ceiling + pitch cap; returns False → skip
       d. Build pitch context (vertical-keyed attack angles, tx details)
       e. XRPL payment: send_demo_payment_for_usdc() — fire-and-forget
       f. Render pitch: templates.render_pitch() with tx hash in context
       g. SMTP send: dispatcher.send_pitch()
       h. State transitions: record_payment_sent() → record_pitch_delivered()
  3. Verification pass: check existing PITCH_DELIVERED domains with verifier

Environment:
  BB7_XRPL_WALLET_SEED   — hot wallet seed (required for payment step)
  BB7_SMTP_HOST/USER/PASS — SMTP credentials (required for send step)
  BB7_XRP_PRICE_USDC     — optional; defaults to 0.50 USD/XRP
  BEAST_OUTPUT_ROOT       — optional; defaults to ./output
  BB7_OPT_OUT_URL         — public opt-out URL (required)
  BB7_OPERATOR_SIGNATURE  — operator name in pitch footer (required)

Dry-run mode: set BB7_OUTREACH_DRY_RUN=1 to simulate the full cycle
without sending SMTP or submitting XRPL transactions.
"""

import json
import logging
import os
import time
from pathlib import Path

from .dispatcher import DispatchError, send_pitch
from .enricher import enrich_domain
from .guardrails import (
    OUTREACH_STANDARD_FEE_USDC,
    SystemHaltedException,
    _output_root,
)
from .state import OutreachStateMachine
from .templates import (
    TemplateMissingVariableError,
    observation_for_attack_angles,
    render_pitch,
)
from .verifier import check_link, record_result

logger = logging.getLogger("sml-beast.outreach.agent")

# ── constants ────────────────────────────────────────────────────────────────

DEFAULT_VERTICALS = ("mastersheets", "xrpl_x402")

# Anchor URLs per vertical — the SML pages we want linked
ANCHOR_URLS: dict[str, str] = {
    "mastersheets": "https://www.scriptmasterlabs.com/mastersheets",
    "xrpl_x402": "https://www.scriptmasterlabs.com/infrastructure",
}

ANCHOR_TITLES: dict[str, str] = {
    "mastersheets": "MasterSheets — sovereign spreadsheet platform",
    "xrpl_x402": "SML Institutional Rails — sub-50ms M2M payment infrastructure",
}

_DRY_RUN = os.environ.get("BB7_OUTREACH_DRY_RUN", "").strip() == "1"


# ── target loader ────────────────────────────────────────────────────────────


def _load_targets(vertical: str, output_root: Path) -> list[dict]:
    """Load bounty_targets.json for a vertical. Returns [] on missing/corrupt."""
    p = output_root / vertical / "bounty_targets.json"
    if not p.exists():
        logger.info("no bounty_targets.json for vertical=%s (run BB4 first)", vertical)
        return []
    try:
        with open(p) as f:
            data = json.load(f)
        targets = data.get("targets", [])
        # Sort by priority_score descending; highest-value targets first
        targets.sort(key=lambda t: t.get("priority_score", 0), reverse=True)
        return targets
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("failed to load bounty_targets for %s: %s", vertical, e)
        return []


# ── verification pass ────────────────────────────────────────────────────────


def _run_verification_pass(sm: OutreachStateMachine, output_root: Path) -> None:
    """Check all PITCH_DELIVERED domains for live link presence."""
    snap = sm.snapshot()
    for domain, entry in snap.get("domains", {}).items():
        if entry.get("state") != "PITCH_DELIVERED":
            continue
        target_url = entry.get("target_url")
        vertical = entry.get("vertical")
        if not target_url or not vertical:
            continue
        anchor_url = ANCHOR_URLS.get(vertical, "")
        if not anchor_url:
            continue

        checked_at = entry.get("last_verified_at_utc", 0)
        # Weekly cadence: skip if checked less than 6 days ago
        if checked_at and (int(time.time()) - checked_at) < (6 * 86400):
            continue

        result = check_link(domain, target_url, anchor_url)
        record_result(result, output_root)
        logger.info(
            "verification: domain=%s found=%s nofollow=%s",
            domain,
            result.found,
            result.nofollow,
        )


# ── single-domain dispatch ────────────────────────────────────────────────────


def _dispatch_one(
    target: dict,
    vertical: str,
    sm: OutreachStateMachine,
    xrpl_client,
    opt_out_url: str,
    operator_signature: str,
) -> bool:
    """Run the full dispatch sequence for one target. Returns True on success."""
    domain = target.get("domain", "").strip().lower()
    if not domain:
        return False

    # Step 1: Enrich
    enrichment = enrich_domain(domain)
    if not enrichment.enriched:
        logger.info("skip %s: no contact found (source=%s)", domain, enrichment.source)
        return False

    recipient_email: str = enrichment.email  # type: ignore[assignment]  # guarded by enrichment.enriched

    # Step 2: Manual review gate
    if sm.needs_manual_review(vertical):
        logger.warning(
            "MANUAL REVIEW REQUIRED for vertical=%s domain=%s — operator must "
            "call record_manual_review_completed() to clear gate",
            vertical,
            domain,
        )
        return False

    # Step 3: atomic_reserve_pitch — kills switch + blocklist + cooldown + ceiling
    try:
        reserved = sm.atomic_reserve_pitch(domain, vertical, OUTREACH_STANDARD_FEE_USDC)
    except SystemHaltedException:
        logger.error("kill switch active — aborting all outreach")
        raise

    if not reserved:
        logger.info("skip %s: denied by guardrails (cooldown / ceiling / blocklist)", domain)
        return False

    # Step 4: XRPL payment (fire-and-forget)
    anchor_url = ANCHOR_URLS.get(vertical, "")
    if _DRY_RUN:
        tx_hash = "DRY_RUN_NO_TX"
        settlement_ms = 0
        logger.info("[DRY RUN] skipping XRPL payment for %s", domain)
    else:
        t0 = time.monotonic()
        try:
            tx_hash, _xrp_sent = xrpl_client.send_demo_payment_for_usdc(
                recipient_email,
                OUTREACH_STANDARD_FEE_USDC,
                memo=f"bb7-demo-{domain}",
            )
        except Exception as e:
            logger.error("XRPL payment failed for %s: %s", domain, e)
            return False
        settlement_ms = int((time.monotonic() - t0) * 1000)

    sm.record_payment_sent(domain, tx_hash)

    # Step 5: Build pitch context
    attack_angles = target.get("attack_angles", [])
    observation = observation_for_attack_angles(attack_angles, vertical)
    gap_finding = target.get("top_keyword", domain)

    # Derive a site name and first name from domain (best-effort)
    site_name = domain.split(".")[0].capitalize()
    first_name = "Team"

    context = {
        "first_name_or_team_handle": first_name,
        "pers_observation": observation,
        "pers_gap_finding": gap_finding,
        "usdc_amount": f"{OUTREACH_STANDARD_FEE_USDC:.2f}",
        "enrichment_source": enrichment.source or "contact page",
        "xrpl_tx_hash": tx_hash,
        "settlement_time_ms": str(settlement_ms),
        "anchor_url": anchor_url,
        "anchor_resource_title": ANCHOR_TITLES.get(vertical, "ScriptMasterLabs"),
        "pers_target_url": f"https://{domain}/",
        "opt_out_url": opt_out_url,
        "operator_signature": operator_signature,
        "domain": domain,
        "site_name": site_name,
    }

    # Step 6: Render pitch
    try:
        pitch = render_pitch(vertical, context)
    except TemplateMissingVariableError as e:
        logger.error("template error for %s: %s", domain, e)
        return False

    # Step 7: SMTP send
    if _DRY_RUN:
        logger.info(
            "[DRY RUN] would send pitch to %s for domain %s\nSubject: %s",
            recipient_email,
            domain,
            pitch.subject,
        )
        sm.record_pitch_delivered(domain)
        return True

    try:
        dispatch_result = send_pitch(pitch, recipient_email)
    except DispatchError as e:
        logger.error("SMTP failed for %s: %s", domain, e)
        return False

    # Step 8: Record Message-ID → domain for the reply monitor's thread map
    from .reply_monitor import record_dispatch
    record_dispatch(dispatch_result.message_id, domain)

    # Step 9: State transition
    sm.record_pitch_delivered(domain)
    logger.info(
        "PITCH DELIVERED: domain=%s vertical=%s tx=%s msg_id=%s",
        domain,
        vertical,
        tx_hash,
        dispatch_result.message_id,
    )
    return True


# ── main cycle ───────────────────────────────────────────────────────────────


def run_cycle(
    verticals: tuple[str, ...] = DEFAULT_VERTICALS,
    output_root: Path | None = None,
    xrpl_client=None,
    balance_check_fn=None,
) -> dict:
    """Run one full outreach cycle. Returns a summary dict.

    Parameters
    ----------
    verticals
        Which verticals to process (default: all configured).
    output_root
        Override for output directory (default: BEAST_OUTPUT_ROOT / ./output).
    xrpl_client
        Injectable XRPLClient for test isolation. If None and not dry-run,
        constructs a real client from BB7_XRPL_WALLET_SEED.

    Returns a dict:
      {
        "vertical": {"mastersheets": {"attempted": N, "sent": N}, ...},
        "total_attempted": N,
        "total_sent": N,
        "dry_run": bool,
      }
    """
    root = output_root or _output_root()
    sm = OutreachStateMachine()

    opt_out_url = os.environ.get("BB7_OPT_OUT_URL", "https://www.scriptmasterlabs.com/outreach/opt-out")
    operator_signature = os.environ.get("BB7_OPERATOR_SIGNATURE", "ScriptMasterLabs")

    # Lazy XRPL client construction (skipped in dry-run)
    if xrpl_client is None and not _DRY_RUN:
        from .xrpl_client import XRPLClient
        xrpl_client = XRPLClient()

    summary: dict = {
        "vertical": {},
        "total_attempted": 0,
        "total_sent": 0,
        "dry_run": _DRY_RUN,
        "balance_check": None,
    }

    # Pre-flight gate — verify hot wallet has headroom before spending. The
    # check is best-effort; a network error returns a degraded result and we
    # proceed (operator will see the warning via alerts + logs). Only a
    # confirmed unhealthy balance aborts the cycle.
    # Injectable balance_check_fn allows tests to skip the network round-trip.
    if not _DRY_RUN and balance_check_fn is not None:
        try:
            bal = balance_check_fn()
            summary["balance_check"] = bal.to_dict() if hasattr(bal, "to_dict") else bal
            if hasattr(bal, "healthy") and bal.error is None and not bal.healthy:
                logger.error(
                    "ABORT cycle: hot wallet balance %.2f USDC below threshold",
                    bal.usdc_equiv,
                )
                return summary
        except Exception as e:
            logger.warning("balance pre-flight failed: %s — proceeding cautiously", e)
    elif not _DRY_RUN:
        try:
            from .balance import check_and_alert_if_low
            bal = check_and_alert_if_low()
            summary["balance_check"] = bal.to_dict()
            if bal.error is None and not bal.healthy:
                logger.error(
                    "ABORT cycle: hot wallet balance %.2f USDC below threshold",
                    bal.usdc_equiv,
                )
                return summary
        except Exception as e:
            logger.warning("balance pre-flight failed: %s — proceeding cautiously", e)

    for vertical in verticals:
        targets = _load_targets(vertical, root)
        attempted = 0
        sent = 0

        for target in targets:
            attempted += 1
            try:
                ok = _dispatch_one(
                    target, vertical, sm, xrpl_client, opt_out_url, operator_signature
                )
                if ok:
                    sent += 1
            except SystemHaltedException:
                logger.error("kill switch tripped — stopping all verticals")
                summary["vertical"][vertical] = {"attempted": attempted, "sent": sent}
                summary["total_attempted"] += attempted
                summary["total_sent"] += sent
                return summary
            except Exception as e:
                logger.error("unexpected error dispatching %s: %s", target.get("domain"), e)

        summary["vertical"][vertical] = {"attempted": attempted, "sent": sent}
        summary["total_attempted"] += attempted
        summary["total_sent"] += sent

    # Verification pass (observation-only, no money movement)
    try:
        _run_verification_pass(sm, root)
    except Exception as e:
        logger.warning("verification pass error: %s", e)

    logger.info(
        "cycle complete: attempted=%d sent=%d dry_run=%s",
        summary["total_attempted"],
        summary["total_sent"],
        _DRY_RUN,
    )

    # Operator notifications — best-effort, never breaks the cycle.
    try:
        from .alerts import alert_cycle_complete, check_and_alert

        check_and_alert()
        alert_cycle_complete(summary["total_attempted"], summary["total_sent"])
    except Exception as e:
        logger.warning("alert dispatch error: %s", e)

    return summary


# ── CLI entry ────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    result = run_cycle()
    print(json.dumps(result, indent=2))
