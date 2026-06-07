"""
BB7 verifier — observation-only link presence checker.

Per BB7_DESIGN.md §5.3: the verifier is COMPLETELY DECOUPLED from
money movement. No fund release. No EscrowFinish. No clawback. This
module is pure analytics — it answers "did the link land?" and logs
the result. Callers act on that information; the verifier does not.

Operational contract:
  - Checks are triggered on a weekly cadence for 90 days post-pitch
  - If a link is found then later removed during the 90-day window,
    the domain is added to a 365-day cooldown-blocklist entry (no
    future agent pitches); the USDC is gone — we don't follow up
  - Results land in output/_internal/conversion_metrics.jsonl
    (append-only; operator-only path)
  - Dashboard surfaces aggregate conversion rates from this log

Link-presence detection:
  1. Fetch the exact pitched URL (full URL, not just the domain)
  2. Parse the DOM for `<a href>` attributes pointing to scriptmasterlabs.com
  3. Record: href, rel attributes (dofollow/nofollow), anchor text match
  4. Wildcard-DNS guard: if the domain DNS resolves but the fetched body
     is < 500 chars or missing HTML structure, classify as SUSPICIOUS
     and route to manual review

Public interface:
  check_link(domain, target_url, anchor_url) -> LinkCheckResult
  record_result(result, output_root) -> None
  load_metrics(output_root) -> list[dict]
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .guardrails import _output_root

logger = logging.getLogger("sml-beast.outreach.verifier")

# ── constants ────────────────────────────────────────────────────────────────

MONITOR_WINDOW_DAYS = 90
LINK_REMOVED_COOLDOWN_DAYS = 365
_SML_DOMAIN = "scriptmasterlabs.com"
_SUSPICIOUS_BODY_MIN_LEN = 500

HTTP_TIMEOUT_S = 10
HTTP_HEADERS = {
    "User-Agent": (
        "scriptmasterlabs-verifier/1.0 "
        "(+https://scriptmasterlabs.com/.well-known/security.txt)"
    ),
}

METRICS_FILE = "_internal/conversion_metrics.jsonl"


# ── result type ──────────────────────────────────────────────────────────────


@dataclass
class LinkCheckResult:
    domain: str
    target_url: str          # the exact URL pitched (e.g., https://example.com/post-123)
    anchor_url: str          # SML URL we want linked (e.g., https://scriptmasterlabs.com/mastersheets)
    checked_at_utc: int = field(default_factory=lambda: int(time.time()))

    # populated by check_link()
    found: bool = False
    nofollow: bool = False   # True if rel="nofollow" or rel="ugc/sponsored"
    anchor_text: str = ""    # anchor text of the matching link
    classification: str = "NOT_FOUND"
    # FOUND | NOT_FOUND | SUSPICIOUS | ERROR
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "target_url": self.target_url,
            "anchor_url": self.anchor_url,
            "checked_at_utc": self.checked_at_utc,
            "found": self.found,
            "nofollow": self.nofollow,
            "anchor_text": self.anchor_text,
            "classification": self.classification,
            "error": self.error,
        }


# ── HTTP helper ──────────────────────────────────────────────────────────────


def _fetch(url: str, *, timeout: int = HTTP_TIMEOUT_S, fetch_fn=None) -> str | None:
    """GET `url`; returns text or None on failure. Injectable for tests."""
    if fetch_fn is not None:
        return fetch_fn(url)
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200:
            return resp.text
    except requests.RequestException as e:
        logger.debug("fetch %s failed: %s", url, e)
    return None


# ── link detection ────────────────────────────────────────────────────────────


def _is_sml_href(href: str, anchor_url: str) -> bool:
    """True if `href` points to scriptmasterlabs.com (any path) or
    matches the exact anchor_url."""
    if not href:
        return False
    parsed = urlparse(href)
    return _SML_DOMAIN in (parsed.netloc or "")


def _parse_links(html: str, anchor_url: str) -> tuple[bool, bool, str]:
    """Return (found, nofollow, anchor_text) for the first SML link in `html`."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a", href=True):
        href: str = a.get("href", "")
        if not _is_sml_href(href, anchor_url):
            continue
        rel: list = a.get("rel", [])
        nofollow = any(r.lower() in ("nofollow", "ugc", "sponsored") for r in rel)
        anchor_text = a.get_text(strip=True)
        return True, nofollow, anchor_text

    return False, False, ""


def _is_suspicious(html: str | None) -> bool:
    """Wildcard-DNS / parking-page guard: body too short or no HTML."""
    if html is None:
        return False
    if len(html) < _SUSPICIOUS_BODY_MIN_LEN:
        return True
    lower = html.lower()
    return "<html" not in lower and "<body" not in lower


# ── public check function ────────────────────────────────────────────────────


def check_link(
    domain: str,
    target_url: str,
    anchor_url: str,
    *,
    fetch_fn=None,
) -> LinkCheckResult:
    """Fetch `target_url` and detect whether a link to `anchor_url` is present.

    Injectable `fetch_fn(url) -> str | None` for test isolation.
    Never raises — errors are captured in result.error and
    result.classification = "ERROR"."""
    result = LinkCheckResult(domain=domain, target_url=target_url, anchor_url=anchor_url)

    try:
        html = _fetch(target_url, fetch_fn=fetch_fn)
    except Exception as e:
        result.classification = "ERROR"
        result.error = str(e)
        return result

    if html is None:
        result.classification = "ERROR"
        result.error = f"fetch returned None for {target_url}"
        return result

    if _is_suspicious(html):
        result.classification = "SUSPICIOUS"
        logger.warning("suspicious response for %s — wildcard DNS?", target_url)
        return result

    found, nofollow, anchor_text = _parse_links(html, anchor_url)
    result.found = found
    result.nofollow = nofollow
    result.anchor_text = anchor_text
    result.classification = "FOUND" if found else "NOT_FOUND"

    logger.info(
        "link check: domain=%s found=%s nofollow=%s anchor=%r",
        domain,
        found,
        nofollow,
        anchor_text,
    )
    return result


# ── persistence ───────────────────────────────────────────────────────────────


def _metrics_path(output_root: Path | None = None) -> Path:
    root = output_root or _output_root()
    return root / METRICS_FILE


def record_result(result: LinkCheckResult, output_root: Path | None = None) -> None:
    """Append a LinkCheckResult to the conversion_metrics.jsonl log.

    Append-only; creates parent directories if absent.
    Uses line-buffered writes — each JSON record is one line."""
    p = _metrics_path(output_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(result.to_dict()) + "\n")


def load_metrics(output_root: Path | None = None) -> list[dict]:
    """Read all records from conversion_metrics.jsonl.

    Skips unparseable lines (corruption guard). Returns [] if the file
    does not yet exist."""
    p = _metrics_path(output_root)
    if not p.exists():
        return []
    records: list[dict] = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("skipping corrupt metrics line: %s", line[:80])
    return records


# ── aggregate stats ───────────────────────────────────────────────────────────


def conversion_stats(output_root: Path | None = None) -> dict:
    """Return aggregate conversion metrics for the dashboard panel.

    {
      "total_checks": int,
      "found": int,
      "not_found": int,
      "suspicious": int,
      "error": int,
      "dofollow_rate": float,   # among FOUND, fraction that are dofollow
      "conversion_rate": float, # found / (found + not_found), ignoring suspicious+error
    }
    """
    records = load_metrics(output_root)
    total = len(records)
    found = sum(1 for r in records if r.get("classification") == "FOUND")
    not_found = sum(1 for r in records if r.get("classification") == "NOT_FOUND")
    suspicious = sum(1 for r in records if r.get("classification") == "SUSPICIOUS")
    error = sum(1 for r in records if r.get("classification") == "ERROR")
    dofollow_found = sum(
        1 for r in records if r.get("classification") == "FOUND" and not r.get("nofollow")
    )
    eligible = found + not_found
    return {
        "total_checks": total,
        "found": found,
        "not_found": not_found,
        "suspicious": suspicious,
        "error": error,
        "dofollow_rate": (dofollow_found / found) if found else 0.0,
        "conversion_rate": (found / eligible) if eligible else 0.0,
    }
