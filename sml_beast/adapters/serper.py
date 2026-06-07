"""
Live SERP client — Serper.dev.

Real data only. No mock results, no synthetic SERPs. If SERPER_API_KEY is
unset the client raises; the orchestrator surfaces the failure rather than
fabricating data.
"""

import os
import time

import requests


class SerperError(RuntimeError):
    pass


class SerperClient:
    BASE = "https://google.serper.dev/search"

    def __init__(self, api_key: str | None = None, timeout: int = 15):
        self.api_key = api_key or os.environ.get("SERPER_API_KEY", "")
        if not self.api_key:
            raise SerperError("SERPER_API_KEY missing — refusing to fabricate SERP data")
        self.timeout = timeout

    def search(self, query: str, gl: str = "us", hl: str = "en", num: int = 10) -> dict:
        r = requests.post(
            self.BASE,
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            json={"q": query, "gl": gl, "hl": hl, "num": num},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise SerperError(f"serper {r.status_code}: {r.text[:200]}")
        return {
            "query": query,
            "fetched_at": time.time(),
            "organic": r.json().get("organic", []),
            "people_also_ask": r.json().get("peopleAlsoAsk", []),
            "related": r.json().get("relatedSearches", []),
            "raw": r.json(),
        }
