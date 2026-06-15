"""
Worker abstract base. Each vertical subclasses Worker, declares its
keyword silos + product brief, and implements `process_keyword`.

The orchestrator fans out one thread per worker. Inside each thread the
loop is: live SERP → GapReport → page-brief synthesis → MDX + JSON-LD.
The canonical brief is never passed to the generator directly — the
synthesized per-page brief (with the gap overlay) is the only object the
generator sees at runtime. That's the feedback loop.
"""

import logging
import os
import threading
import time
from abc import ABC, abstractmethod

import requests

from sml_beast.adapters.x402_proxy import mint_internal_token
from sml_beast.intel.backlink_targets import BacklinkTargetFinder
from sml_beast.intel.serp_gap import analyze, synthesize_page_brief

logger = logging.getLogger("sml-beast.worker")


class Worker(ABC):
    name: str = "base"
    vertical: str = "base"  # stable key used by the gap engine + JSON-LD factory
    BRAND_DOMAINS: tuple[str, ...] = ("scriptmasterlabs.com",)
    MIN_PRIORITY: int = 25  # skip keywords whose gap doesn't clear this bar
    PROXY_PATH: str = "/api/v1/m2m/serp"

    def __init__(
        self, brief: dict, silos: dict, proxy_url: str, output_dir: str, stop: threading.Event
    ):
        self.brief = brief
        self.silos = silos
        self.proxy_url = proxy_url.rstrip("/")
        self.output_dir = output_dir
        self.stop = stop
        self.backlinks = BacklinkTargetFinder(brand_domains=self.BRAND_DOMAINS)

    def serp(self, query: str) -> dict:
        token = mint_internal_token(wallet=f"beast-{self.name}")
        r = requests.post(
            f"{self.proxy_url}{self.PROXY_PATH}",
            json={"q": query, "num": 10},
            headers={"X-PAYMENT": token, "Content-Type": "application/json"},
            timeout=20,
        )
        if r.status_code == 402:
            raise RuntimeError(f"x402 proxy rejected payment: {r.text[:200]}")
        if r.status_code != 200:
            raise RuntimeError(f"x402 proxy {r.status_code}: {r.text[:200]}")
        return r.json().get("result", {})

    @abstractmethod
    def process_keyword(self, silo_name: str, keyword: str, page_brief: dict) -> str:
        """Generate the artifact and return the path written."""
        ...

    def run(self):
        logger.info("[%s] worker starting — %d silos", self.name, len(self.silos))
        skipped = 0
        for silo_name, keywords in self.silos.items():
            for kw in keywords:
                if self.stop.is_set():
                    logger.info("[%s] stop requested; exiting", self.name)
                    self.backlinks.flush(self.vertical, self.output_dir)
                    return
                try:
                    data = self.serp(kw)
                    # Harvest backlink targets BEFORE the priority gate — a
                    # low-priority SERP still surfaces valuable placement
                    # domains for the M2M bounty list.
                    self.backlinks.ingest(data, kw)
                    gap = analyze(data, brand_domains=self.BRAND_DOMAINS, vertical=self.vertical)
                    if gap.priority_score < self.MIN_PRIORITY:
                        skipped += 1
                        logger.info(
                            "[%s] skip %-40s priority=%d severity=%s",
                            self.name,
                            kw,
                            gap.priority_score,
                            gap.gap_severity,
                        )
                        continue
                    page_brief = synthesize_page_brief(self.brief, gap, vertical=self.vertical)
                    path = self.process_keyword(silo_name, kw, page_brief)
                    logger.info(
                        "[%s] %-40s priority=%d severity=%-8s -> %s",
                        self.name,
                        kw,
                        gap.priority_score,
                        gap.gap_severity,
                        path,
                    )
                except Exception as e:
                    logger.error("[%s] %s failed: %s", self.name, kw, e)
                time.sleep(0.6)
            # End of silo — flush the bounty list so progress survives crashes
            # and downstream agents can pick up partial results mid-run.
            bounty_path = self.backlinks.flush(self.vertical, os.path.dirname(self.output_dir))
            logger.info(
                "[%s] silo %s complete — bounty list: %s", self.name, silo_name, bounty_path
            )
        logger.info("[%s] worker complete (skipped %d low-priority)", self.name, skipped)
