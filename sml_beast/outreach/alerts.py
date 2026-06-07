"""
BB7 operator alerts — Discord webhook notifications for critical events.

The dashboard requires the operator to be logged in. This module pushes
high-priority events directly to a Discord channel so the operator finds
out about failures even when not actively watching.

Alert types (each has its own severity + idempotency rule):
  KILL_SWITCH_ACTIVATED  — fires once per session when the kill switch file
                           appears. Won't re-alert unless deactivated first.
  KILL_SWITCH_DEACTIVATED — fires when the kill switch file disappears.
  LOW_HOT_WALLET         — fires when hot wallet < threshold (XRPL balance query).
                           Rate-limited to once per 24h to avoid spam.
  MANUAL_REVIEW_BACKLOG  — fires when operator queue grows past N entries.
                           Rate-limited to once per 24h.
  CYCLE_COMPLETE         — informational; fires after each agent.run_cycle().
                           Not rate-limited.

The operator sets BB7_DISCORD_ALERT_WEBHOOK to enable alerts. If unset,
this module silently does nothing — no alerts is acceptable; throwing on
missing config would break the agent cycle on a config typo.

Rate-limit state lives in output/_internal/alert_state.json (atomic write
via temp-file-and-rename, same pattern as state.py and reply monitor).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import requests

from .guardrails import _output_root

logger = logging.getLogger("sml-beast.outreach.alerts")

# ── constants ────────────────────────────────────────────────────────────────

ALERT_STATE_FILE = "_internal/alert_state.json"
RATE_LIMIT_S = 86400  # 24h — minimum interval between duplicate alerts
DISCORD_TIMEOUT_S = 10

LOW_HOT_WALLET_THRESHOLD_USDC = 20.0  # below this, alert
MANUAL_REVIEW_BACKLOG_THRESHOLD = 5    # at/above this entries, alert


# ── alert types ──────────────────────────────────────────────────────────────


class AlertType(Enum):
    KILL_SWITCH_ACTIVATED = "kill_switch_activated"
    KILL_SWITCH_DEACTIVATED = "kill_switch_deactivated"
    LOW_HOT_WALLET = "low_hot_wallet"
    MANUAL_REVIEW_BACKLOG = "manual_review_backlog"
    CYCLE_COMPLETE = "cycle_complete"


# Per-alert severity → Discord embed color (decimal RGB).
_SEVERITY_COLOR = {
    AlertType.KILL_SWITCH_ACTIVATED: 0xFF0000,    # red
    AlertType.KILL_SWITCH_DEACTIVATED: 0x00FF66,  # neon green
    AlertType.LOW_HOT_WALLET: 0xFFB000,           # amber
    AlertType.MANUAL_REVIEW_BACKLOG: 0xFFB000,    # amber
    AlertType.CYCLE_COMPLETE: 0x00FFFF,           # neon cyan
}

# Alerts that are rate-limited to once per RATE_LIMIT_S.
_RATE_LIMITED = {
    AlertType.LOW_HOT_WALLET,
    AlertType.MANUAL_REVIEW_BACKLOG,
}


@dataclass
class AlertResult:
    sent: bool
    reason: str  # "ok" | "no_webhook" | "rate_limited" | "http_error"


# ── rate-limit state ─────────────────────────────────────────────────────────


def _alert_state_path(output_root: Path | None = None) -> Path:
    return (output_root or _output_root()) / ALERT_STATE_FILE


def _load_alert_state(output_root: Path | None = None) -> dict:
    p = _alert_state_path(output_root)
    if not p.exists():
        return {}
    try:
        with open(p) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        logger.warning("alert state unreadable; treating as empty")
        return {}


def _save_alert_state(state: dict, output_root: Path | None = None) -> None:
    p = _alert_state_path(output_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, p)


def _can_send(
    alert_type: AlertType, state: dict, now: int
) -> bool:
    """Return True if rate-limit allows this alert to fire."""
    if alert_type not in _RATE_LIMITED:
        return True
    last = state.get(alert_type.value, 0)
    return (now - last) >= RATE_LIMIT_S


# ── Discord webhook ──────────────────────────────────────────────────────────


def _post_to_discord(
    webhook_url: str,
    alert_type: AlertType,
    title: str,
    description: str,
    fields: dict | None = None,
    *,
    post_fn=None,
) -> bool:
    """POST one embed to a Discord webhook. Returns True on success.

    Injectable post_fn for test isolation. Default uses requests.post."""
    embed = {
        "title": title,
        "description": description,
        "color": _SEVERITY_COLOR[alert_type],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if fields:
        embed["fields"] = [
            {"name": k, "value": str(v), "inline": True} for k, v in fields.items()
        ]
    payload = {
        "username": "BB7 Outreach",
        "embeds": [embed],
    }

    poster = post_fn or requests.post
    try:
        resp = poster(webhook_url, json=payload, timeout=DISCORD_TIMEOUT_S)
        # Discord returns 204 No Content on success
        if hasattr(resp, "status_code"):
            return 200 <= resp.status_code < 300
        return True
    except Exception as e:
        logger.warning("Discord webhook post failed: %s", e)
        return False


# ── public emit function ─────────────────────────────────────────────────────


def emit(
    alert_type: AlertType,
    *,
    title: str | None = None,
    description: str = "",
    fields: dict | None = None,
    output_root: Path | None = None,
    post_fn=None,
    webhook_url: str | None = None,
) -> AlertResult:
    """Emit an alert. Honors rate-limit + missing-webhook gracefully.

    `webhook_url` overrides the env var (used in tests). When unset, reads
    BB7_DISCORD_ALERT_WEBHOOK from the environment."""
    url = webhook_url if webhook_url is not None else os.environ.get(
        "BB7_DISCORD_ALERT_WEBHOOK", ""
    ).strip()
    if not url:
        logger.debug("no webhook configured; alert %s skipped", alert_type.value)
        return AlertResult(sent=False, reason="no_webhook")

    state = _load_alert_state(output_root)
    now = int(time.time())

    if not _can_send(alert_type, state, now):
        logger.info("alert %s rate-limited", alert_type.value)
        return AlertResult(sent=False, reason="rate_limited")

    final_title = title or _default_title(alert_type)
    sent = _post_to_discord(
        url, alert_type, final_title, description, fields, post_fn=post_fn
    )
    if sent:
        state[alert_type.value] = now
        _save_alert_state(state, output_root)
        logger.info("alert sent: %s", alert_type.value)
        return AlertResult(sent=True, reason="ok")
    return AlertResult(sent=False, reason="http_error")


def _default_title(alert_type: AlertType) -> str:
    return {
        AlertType.KILL_SWITCH_ACTIVATED: "BB7 KILL SWITCH ACTIVATED",
        AlertType.KILL_SWITCH_DEACTIVATED: "BB7 kill switch DEACTIVATED",
        AlertType.LOW_HOT_WALLET: "BB7 hot wallet low",
        AlertType.MANUAL_REVIEW_BACKLOG: "BB7 manual review backlog",
        AlertType.CYCLE_COMPLETE: "BB7 cycle complete",
    }[alert_type]


# ── pre-built alert helpers ──────────────────────────────────────────────────


def alert_kill_switch_activated(
    *, output_root: Path | None = None, post_fn=None
) -> AlertResult:
    """Operator activated the kill switch — all dispatch halted."""
    return emit(
        AlertType.KILL_SWITCH_ACTIVATED,
        description=(
            "All new outreach dispatch is **HALTED**. "
            "Verification loop continues; existing state is preserved. "
            "Remove the kill switch file or run `opctl kill off` to resume."
        ),
        output_root=output_root,
        post_fn=post_fn,
    )


def alert_kill_switch_deactivated(
    *, output_root: Path | None = None, post_fn=None
) -> AlertResult:
    return emit(
        AlertType.KILL_SWITCH_DEACTIVATED,
        description="Outreach can now dispatch again on the next cycle.",
        output_root=output_root,
        post_fn=post_fn,
    )


def alert_low_hot_wallet(
    balance_usdc: float, *, output_root: Path | None = None, post_fn=None
) -> AlertResult:
    return emit(
        AlertType.LOW_HOT_WALLET,
        description=(
            f"Hot wallet balance is **{balance_usdc:.2f} USDC**, below the "
            f"safety threshold of {LOW_HOT_WALLET_THRESHOLD_USDC:.2f} USDC. "
            "Refill from the cold wallet before the next cycle."
        ),
        fields={"balance_usdc": f"{balance_usdc:.2f}"},
        output_root=output_root,
        post_fn=post_fn,
    )


def alert_manual_review_backlog(
    count: int, *, output_root: Path | None = None, post_fn=None
) -> AlertResult:
    return emit(
        AlertType.MANUAL_REVIEW_BACKLOG,
        description=(
            f"There are **{count}** entries in the operator review queue "
            f"awaiting your attention. Run `opctl replies` to inspect."
        ),
        fields={"queue_size": str(count)},
        output_root=output_root,
        post_fn=post_fn,
    )


def alert_cycle_complete(
    attempted: int, sent: int, *, output_root: Path | None = None, post_fn=None
) -> AlertResult:
    return emit(
        AlertType.CYCLE_COMPLETE,
        description=f"Outreach cycle finished: attempted={attempted}, sent={sent}.",
        fields={"attempted": str(attempted), "sent": str(sent)},
        output_root=output_root,
        post_fn=post_fn,
    )


# ── periodic check helper ────────────────────────────────────────────────────


def check_and_alert(
    *, output_root: Path | None = None, post_fn=None
) -> list[AlertResult]:
    """One-shot sweep: check kill switch, hot wallet, manual-review backlog;
    emit any alerts that fire. Returns the list of results.

    Used by the agent's run_cycle() and by a periodic cron."""
    from .guardrails import kill_switch_path
    from .reply_monitor import load_operator_queue

    results: list[AlertResult] = []

    # Kill switch state (compare to previous run)
    state = _load_alert_state(output_root)
    prev_ks_active = state.get("_last_kill_switch_active", False)
    ks_active = kill_switch_path().exists()

    if ks_active and not prev_ks_active:
        results.append(alert_kill_switch_activated(output_root=output_root, post_fn=post_fn))
    elif (not ks_active) and prev_ks_active:
        results.append(alert_kill_switch_deactivated(output_root=output_root, post_fn=post_fn))

    state["_last_kill_switch_active"] = ks_active
    _save_alert_state(state, output_root)

    # Operator queue backlog
    queue = load_operator_queue(output_root)
    if len(queue) >= MANUAL_REVIEW_BACKLOG_THRESHOLD:
        results.append(
            alert_manual_review_backlog(len(queue), output_root=output_root, post_fn=post_fn)
        )

    return results
