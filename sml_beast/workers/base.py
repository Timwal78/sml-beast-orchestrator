"""
Worker abstract base. Each vertical subclasses Worker, declares its
keyword silos + product brief, and implements `process_keyword`.
The orchestrator fans out one thread per worker; the worker walks its
own keyword space without blocking the other vertical.
"""

import logging
import threading
import time
from abc import ABC, abstractmethod

import requests

from sml_beast.adapters.x402_proxy import mint_internal_token

logger = logging.getLogger("sml-beast.worker")


class Worker(ABC):
    name: str = "base"

    def __init__(self,
                 brief: dict,
                 silos: dict,
                 proxy_url: str,
                 output_dir: str,
                 stop: threading.Event):
        self.brief      = brief
        self.silos      = silos
        self.proxy_url  = proxy_url.rstrip("/")
        self.output_dir = output_dir
        self.stop       = stop

    # ── x402 — query the internal proxy with a freshly minted internal token ──
    def serp(self, query: str) -> dict:
        token = mint_internal_token(wallet=f"beast-{self.name}")
        r = requests.post(
            f"{self.proxy_url}/x402/search",
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
    def process_keyword(self, silo_name: str, keyword: str, serp_data: dict) -> str:
        """Generate the artifact (MDX page, JSON-LD, intel report). Return path written."""
        ...

    # ── thread entry ──────────────────────────────────────────────────────────
    def run(self):
        logger.info("[%s] worker starting — %d silos", self.name, len(self.silos))
        for silo_name, keywords in self.silos.items():
            for kw in keywords:
                if self.stop.is_set():
                    logger.info("[%s] stop requested; exiting", self.name)
                    return
                try:
                    data = self.serp(kw)
                    path = self.process_keyword(silo_name, kw, data)
                    logger.info("[%s] %-40s -> %s", self.name, kw, path)
                except Exception as e:
                    logger.error("[%s] %s failed: %s", self.name, kw, e)
                time.sleep(0.6)   # respect upstream provider rate limits
        logger.info("[%s] worker complete", self.name)
