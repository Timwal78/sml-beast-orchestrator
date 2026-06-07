"""
Backlink-target finder — the M2M bounty list.

Walks every live SERP the orchestrator pulls and accumulates a ranked list
of root domains worth pursuing for backlink placement. The same SERP data
already used by the gap engine is consumed here — zero extra API spend.

Filtering:
  - brand domains (we don't link to ourselves)
  - mega-sites + social platforms (not realistic placement targets)
  - forums (mostly nofollow; harvested separately if needed later)
  - entrenched authority (wikipedia / google / microsoft — not acquirable)

Scoring:
  priority_score = frequency × class_weight
  class weights:
    aggregator   = 4   # capterra / g2 / alternativeto — directories, top placement value
    listicle     = 3   # "10 best X" — replacement targets, high authority transfer
    neutral      = 2   # SaaS blogs, niche review sites — long-tail backlink land
    entrenched   = 0   # filtered upstream; never scored
    forum        = 0   # filtered upstream; never scored

Output (per vertical, JSON):
  output/<vertical>/bounty_targets.json
  {
    generated_at, vertical, total_domains, total_serps_ingested,
    targets: [
      { domain, frequency, class, class_weight, priority_score,
        sample_titles, sample_urls, discovered_via }, ...
    ]
  }

The finder is thread-local — each worker instantiates its own and flushes
to its own vertical directory. Idempotent: re-flushing rewrites the JSON
with the current accumulated state.
"""

import json
import os
import time
from collections import defaultdict
from typing import Iterable
from urllib.parse import urlparse

from .serp_gap import (
    AGGREGATOR_DOMAINS, ENTRENCHED_DOMAINS, FORUM_HOSTS, LISTICLE_RX,
)


# Social platforms + marketplaces + Q&A sites that won't realistically accept
# institutional backlink placement. Entrenched + forums are already filtered
# via the imported sets — this list is the additional exclusion layer.
MEGA_SITES: frozenset[str] = frozenset({
    "youtube.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "tiktok.com", "pinterest.com", "snapchat.com",
    "amazon.com", "amazon.co.uk", "amazon.de", "ebay.com", "etsy.com",
    "yelp.com", "tripadvisor.com",
})

CLASS_WEIGHT: dict[str, int] = {
    "aggregator": 4,
    "listicle":   3,
    "neutral":    2,
    "entrenched": 0,
    "forum":      0,
}

SAMPLE_CAP = 5   # per-domain memory budget for titles/urls/keywords


def _host(url: str) -> str:
    try:
        h = urlparse(url).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _matches(host: str, target_set: Iterable[str]) -> bool:
    for t in target_set:
        if host == t or host.endswith("." + t):
            return True
    return False


def _classify(result: dict) -> str:
    """Classifier for backlink purposes. Aligned with serp_gap._classify but
    not identical — here we collapse 'entrenched' and 'forum' to filter classes
    and treat listicle titles on non-aggregator hosts as their own bucket."""
    host  = _host(result.get("link", ""))
    title = result.get("title", "")
    if _matches(host, ENTRENCHED_DOMAINS):
        return "entrenched"
    if _matches(host, FORUM_HOSTS):
        return "forum"
    if host in AGGREGATOR_DOMAINS:
        return "aggregator"
    if LISTICLE_RX.search(title):
        return "listicle"
    return "neutral"


class BacklinkTargetFinder:
    """Thread-local accumulator. One per worker.

    Lifecycle:
        finder = BacklinkTargetFinder(brand_domains=("scriptmasterlabs.com",))
        for kw in keywords:
            serp = worker.serp(kw)
            finder.ingest(serp, kw)            # called for every SERP, even
                                               # those whose gap was skipped
        finder.flush("mastersheets", "output/")  # writes bounty_targets.json
    """

    def __init__(self, brand_domains: Iterable[str] = ()):
        self.brand = tuple(brand_domains)
        self._domains: dict[str, dict] = defaultdict(lambda: {
            "frequency":      0,
            "class":          "neutral",
            "sample_titles":  [],
            "sample_urls":    [],
            "discovered_via": [],
        })
        self._serps_ingested = 0
        self._lock_kw_set: dict[str, set] = defaultdict(set)

    def ingest(self, serp_data: dict, keyword: str) -> int:
        """Walk top-20 organic; harvest backlink-eligible domains.
        Returns the number of new domains added this call."""
        self._serps_ingested += 1
        added = 0
        for r in serp_data.get("organic", [])[:20]:
            host = _host(r.get("link", ""))
            if not host:
                continue
            if _matches(host, self.brand):
                continue
            if _matches(host, ENTRENCHED_DOMAINS):
                continue
            if _matches(host, FORUM_HOSTS):
                continue
            if _matches(host, MEGA_SITES):
                continue

            entry = self._domains[host]
            if entry["frequency"] == 0:
                added += 1
                entry["class"] = _classify(r)

            entry["frequency"] += 1

            if keyword not in self._lock_kw_set[host]:
                self._lock_kw_set[host].add(keyword)
                if len(entry["discovered_via"]) < SAMPLE_CAP:
                    entry["discovered_via"].append(keyword)

            if len(entry["sample_titles"]) < SAMPLE_CAP:
                t = r.get("title")
                if t and t not in entry["sample_titles"]:
                    entry["sample_titles"].append(t)
            if len(entry["sample_urls"]) < SAMPLE_CAP:
                u = r.get("link")
                if u and u not in entry["sample_urls"]:
                    entry["sample_urls"].append(u)
        return added

    def ranked(self) -> list[dict]:
        """Return the accumulated targets as a list, sorted by priority_score
        desc, ties broken by frequency desc, then domain ascending for stable
        output."""
        out: list[dict] = []
        for domain, entry in self._domains.items():
            weight = CLASS_WEIGHT.get(entry["class"], 1)
            out.append({
                "domain":         domain,
                "frequency":      entry["frequency"],
                "class":          entry["class"],
                "class_weight":   weight,
                "priority_score": entry["frequency"] * weight,
                "sample_titles":  entry["sample_titles"],
                "sample_urls":    entry["sample_urls"],
                "discovered_via": entry["discovered_via"],
            })
        out.sort(key=lambda t: (-t["priority_score"], -t["frequency"], t["domain"]))
        return out

    def flush(self, vertical: str, output_dir: str) -> str:
        """Write the current ranked targets to <output_dir>/<vertical>/
        bounty_targets.json. Returns the file path. Idempotent — safe to call
        from inside the worker loop after every silo cycle."""
        targets = self.ranked()
        out_dir = os.path.join(output_dir, vertical)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "bounty_targets.json")
        payload = {
            "generated_at":         int(time.time()),
            "vertical":             vertical,
            "total_serps_ingested": self._serps_ingested,
            "total_domains":        len(targets),
            "targets":              targets,
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        return path
