"""
BB7 target enricher — contact discovery for outreach domains.

Source priority (highest → lowest), per BB7_DESIGN.md §3:
  1. /.well-known/security.txt  — RFC 9116; Contact: field
  2. /humans.txt                — Contact: / Author: lines
  3. /.well-known/contact.txt   — emerging convention; same tier as 1
  4. /humans.json               — structured; contacts[].email
  5. Author profile pages       — /about, /team, /authors/<slug>
  6. mailto: footer scrape      — DEPRIORITIZED (sponsorship pages only)

Enrichment results are cached to output/enrichment_cache/<domain>.json
with a 30-day TTL. Cache hits skip all HTTP fetches.

Role-based trap accounts that are always rejected:
  abuse@ postmaster@ noreply@ no-reply@ support@ info@ contact@
  webmaster@ admin@ security@ help@ hello@ privacy@ legal@
  unsubscribe@ newsletter@ bounce@ mailer-daemon@

The module is pure-function oriented: EnrichmentResult is a dataclass,
not an object with mutable internal state. Callers own caching via
`enrich_domain()` (the public entry point that handles TTL logic).
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .guardrails import _output_root

logger = logging.getLogger("sml-beast.outreach.enricher")

# ── constants ───────────────────────────────────────────────────────────────

ENRICHMENT_CACHE_TTL_DAYS = 30
_ENRICHMENT_CACHE_TTL_S = ENRICHMENT_CACHE_TTL_DAYS * 86400

HTTP_TIMEOUT_S = 8
HTTP_HEADERS = {
    "User-Agent": (
        "scriptmasterlabs-outreach/1.0 "
        "(+https://scriptmasterlabs.com/.well-known/security.txt)"
    ),
}

# Role-based addresses that reliably bounce or reach spam traps.
ROLE_ACCOUNT_PREFIXES = frozenset(
    {
        "abuse",
        "postmaster",
        "noreply",
        "no-reply",
        "support",
        "info",
        "contact",
        "webmaster",
        "admin",
        "security",
        "help",
        "hello",
        "privacy",
        "legal",
        "unsubscribe",
        "newsletter",
        "bounce",
        "mailer-daemon",
        "root",
        "hostmaster",
    }
)

# Author-profile slugs tried in order when higher-priority sources fail.
AUTHOR_PROFILE_PATHS = ("/about", "/team", "/authors", "/author", "/contact")

# Regex for syntactic email validation — not RFC 5322 complete, but
# catches obvious garbage (missing @, missing TLD, local-only strings).
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$",
    re.ASCII,
)


# ── result dataclass ─────────────────────────────────────────────────────────


@dataclass
class EnrichmentResult:
    domain: str
    email: str | None = None
    source: str | None = None  # which pipeline step found the email
    fetched_at_utc: int = field(default_factory=lambda: int(time.time()))
    raw_contact_lines: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def enriched(self) -> bool:
        return bool(self.email)

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "email": self.email,
            "source": self.source,
            "fetched_at_utc": self.fetched_at_utc,
            "raw_contact_lines": self.raw_contact_lines,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EnrichmentResult":
        return cls(
            domain=d["domain"],
            email=d.get("email"),
            source=d.get("source"),
            fetched_at_utc=d.get("fetched_at_utc", 0),
            raw_contact_lines=d.get("raw_contact_lines", []),
            error=d.get("error"),
        )


# ── email validation ─────────────────────────────────────────────────────────


def is_valid_email(email: str) -> bool:
    """True if `email` passes syntactic validation AND is not a role account."""
    if not email or not isinstance(email, str):
        return False
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        return False
    local = email.split("@")[0]
    return local not in ROLE_ACCOUNT_PREFIXES


# ── HTTP helper ──────────────────────────────────────────────────────────────


def _fetch(url: str, *, timeout: int = HTTP_TIMEOUT_S) -> str | None:
    """GET `url` and return text body, or None on any error."""
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200:
            return resp.text
    except requests.RequestException as e:
        logger.debug("fetch %s failed: %s", url, e)
    return None


# ── source-specific parsers ──────────────────────────────────────────────────


def _parse_security_txt(text: str) -> tuple[str | None, list[str]]:
    """Extract the first valid email from a security.txt Contact: field.

    RFC 9116 allows Contact: mailto:<addr> or Contact: https://... — we
    only extract mailto: values; HTTPS contacts are skipped (they lead
    to web forms, not SMTP addresses)."""
    lines = []
    email = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        if line.lower().startswith("contact:"):
            value = line[len("contact:"):].strip()
            lines.append(value)
            if email is None:
                # Accept mailto:addr or bare addr@domain
                if value.lower().startswith("mailto:"):
                    candidate = value[len("mailto:"):].strip()
                elif "@" in value and not value.startswith("http"):
                    candidate = value.strip()
                else:
                    continue
                if is_valid_email(candidate):
                    email = candidate.lower()
    return email, lines


def _parse_humans_txt(text: str) -> tuple[str | None, list[str]]:
    """Extract Contact: or Author: email from humans.txt."""
    lines = []
    email = None
    for line in text.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("contact:") or lower.startswith("author:"):
            value = stripped.split(":", 1)[1].strip()
            lines.append(value)
            if email is None and "@" in value:
                # May be "Name <addr>" or bare addr
                m = re.search(r"[\w._%+\-]+@[\w.\-]+\.\w+", value)
                if m and is_valid_email(m.group()):
                    email = m.group().lower()
    return email, lines


def _parse_contact_txt(text: str) -> tuple[str | None, list[str]]:
    """Same heuristic as humans.txt — contact.txt has no strict spec."""
    return _parse_humans_txt(text)


def _parse_humans_json(text: str) -> tuple[str | None, list[str]]:
    """Extract first valid email from contacts[].email."""
    try:
        data: Any = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None, []
    lines: list[str] = []
    email: str | None = None
    contacts: list[Any] = []
    if isinstance(data, dict):
        contacts = data.get("contacts") or data.get("team") or []
    elif isinstance(data, list):
        contacts = data
    for c in contacts:
        if not isinstance(c, dict):
            continue
        addr = c.get("email", "")
        if addr:
            lines.append(addr)
            if email is None and is_valid_email(addr):
                email = addr.lower()
    return email, lines


def _parse_author_page(text: str) -> tuple[str | None, list[str]]:
    """Scan an HTML author/about/team page for a business email.

    Only accepts emails that appear in visible text or href="mailto:..."
    attributes — not hidden metadata fields."""
    lines: list[str] = []
    email: str | None = None
    try:
        soup = BeautifulSoup(text, "lxml")
    except Exception:
        soup = BeautifulSoup(text, "html.parser")

    # mailto: hrefs are the most reliable signal on an author page
    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if href.lower().startswith("mailto:"):
            candidate = href[len("mailto:"):].split("?")[0].strip()
            lines.append(candidate)
            if email is None and is_valid_email(candidate):
                email = candidate.lower()

    # Fallback: email-shaped text anywhere in the visible body
    if email is None:
        body_text = soup.get_text(separator=" ")
        for m in re.finditer(r"[\w._%+\-]+@[\w.\-]+\.\w+", body_text):
            candidate = m.group()
            lines.append(candidate)
            if is_valid_email(candidate):
                email = candidate.lower()
                break

    return email, lines


def _parse_footer_for_sponsorship(text: str, base_url: str) -> tuple[str | None, list[str]]:
    """DEPRIORITIZED: only used when the site has a sponsorship/advertise page.
    Scans mailto: hrefs on pages like /advertise, /sponsor, /partner."""
    return _parse_author_page(text)  # same extraction logic


# ── main enrichment pipeline ─────────────────────────────────────────────────


def _base_url(domain: str) -> str:
    """Build https://domain — always prefers HTTPS."""
    if domain.startswith(("http://", "https://")):
        return domain.rstrip("/")
    return f"https://{domain.rstrip('/')}"


def _enrich_live(domain: str) -> EnrichmentResult:
    """Hit the network and run the source priority pipeline."""
    base = _base_url(domain)

    # Priority 1: /.well-known/security.txt
    text = _fetch(urljoin(base, "/.well-known/security.txt"))
    if text:
        email, lines = _parse_security_txt(text)
        if email:
            logger.info("enriched %s via security.txt", domain)
            return EnrichmentResult(
                domain=domain, email=email, source="security.txt", raw_contact_lines=lines
            )

    # Priority 2: /humans.txt
    text = _fetch(urljoin(base, "/humans.txt"))
    if text:
        email, lines = _parse_humans_txt(text)
        if email:
            logger.info("enriched %s via humans.txt", domain)
            return EnrichmentResult(
                domain=domain, email=email, source="humans.txt", raw_contact_lines=lines
            )

    # Priority 3: /.well-known/contact.txt
    text = _fetch(urljoin(base, "/.well-known/contact.txt"))
    if text:
        email, lines = _parse_contact_txt(text)
        if email:
            logger.info("enriched %s via contact.txt", domain)
            return EnrichmentResult(
                domain=domain, email=email, source="contact.txt", raw_contact_lines=lines
            )

    # Priority 4: /humans.json
    text = _fetch(urljoin(base, "/humans.json"))
    if text:
        email, lines = _parse_humans_json(text)
        if email:
            logger.info("enriched %s via humans.json", domain)
            return EnrichmentResult(
                domain=domain, email=email, source="humans.json", raw_contact_lines=lines
            )

    # Priority 5: author profile pages (tried in order, first hit wins)
    for path in AUTHOR_PROFILE_PATHS:
        text = _fetch(urljoin(base, path))
        if text:
            email, lines = _parse_author_page(text)
            if email:
                logger.info("enriched %s via author page %s", domain, path)
                return EnrichmentResult(
                    domain=domain, email=email, source=f"author_page:{path}", raw_contact_lines=lines
                )

    # Priority 6 (DEPRIORITIZED): sponsorship/advertise page mailto: only
    for sponsor_path in ("/advertise", "/sponsor", "/partner", "/sponsorship"):
        text = _fetch(urljoin(base, sponsor_path))
        if text:
            email, lines = _parse_footer_for_sponsorship(text, base)
            if email:
                logger.info("enriched %s via sponsorship page %s", domain, sponsor_path)
                return EnrichmentResult(
                    domain=domain,
                    email=email,
                    source=f"sponsorship_page:{sponsor_path}",
                    raw_contact_lines=lines,
                )

    logger.info("no contact found for %s", domain)
    return EnrichmentResult(domain=domain, email=None, source=None)


# ── cache layer ──────────────────────────────────────────────────────────────


def _cache_dir() -> Path:
    return _output_root() / "enrichment_cache"


def _cache_path(domain: str) -> Path:
    # Sanitize domain for use as a filename
    safe = re.sub(r"[^a-zA-Z0-9.\-_]", "_", domain)
    return _cache_dir() / f"{safe}.json"


def _load_cached(domain: str) -> EnrichmentResult | None:
    p = _cache_path(domain)
    if not p.exists():
        return None
    try:
        with open(p) as f:
            data = json.load(f)
        result = EnrichmentResult.from_dict(data)
        age_s = int(time.time()) - result.fetched_at_utc
        if age_s > _ENRICHMENT_CACHE_TTL_S:
            logger.debug("cache expired for %s (%dd)", domain, age_s // 86400)
            return None
        return result
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def _save_cached(result: EnrichmentResult) -> None:
    p = _cache_path(result.domain)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(result.to_dict(), f, indent=2, sort_keys=True)
    import os
    os.replace(tmp, p)


# ── public entry point ───────────────────────────────────────────────────────


def enrich_domain(domain: str, *, force_refresh: bool = False) -> EnrichmentResult:
    """Return an EnrichmentResult for `domain`.

    Serves from cache if the cached entry is within ENRICHMENT_CACHE_TTL_DAYS.
    Set `force_refresh=True` to bypass cache and hit the network regardless.

    Never raises — on network or parse failure, returns an EnrichmentResult
    with email=None and error set. Callers decide how to handle un-enriched
    targets (typically: skip the domain for this cycle)."""
    domain = domain.strip().lower()
    if not domain:
        return EnrichmentResult(domain=domain, error="empty domain")

    if not force_refresh:
        cached = _load_cached(domain)
        if cached is not None:
            logger.debug("cache hit for %s", domain)
            return cached

    try:
        result = _enrich_live(domain)
    except Exception as e:
        logger.warning("enrichment failed for %s: %s", domain, e)
        result = EnrichmentResult(domain=domain, error=str(e))

    _save_cached(result)
    return result
