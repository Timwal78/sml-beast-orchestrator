"""Tests for sml_beast/outreach/enricher.py — contact discovery pipeline.

All HTTP is mocked via unittest.mock.patch so no real network calls occur.
Cache reads/writes use a tmpdir per test (BEAST_OUTPUT_ROOT env var).

Covers:
  - Email validation: syntactic rules, role-account rejection
  - security.txt parser: mailto: forms, bare address, HTTPS skipped
  - humans.txt parser: Contact: and Author: lines, name+addr format
  - contact.txt parser (same as humans.txt)
  - humans.json parser: contacts[].email, list-root form, bad JSON
  - Author page parser: mailto: hrefs, visible text fallback
  - Sponsorship page parser (deprioritized path)
  - Source priority waterfall: first hit wins
  - Cache TTL: hit within 30d, miss after 30d, force_refresh bypasses
  - Atomic write: no .tmp file left behind
  - Empty / error domain handling
"""

import importlib
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch


class _EnricherBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beast-enricher-")
        os.environ["BEAST_OUTPUT_ROOT"] = self.tmp
        # Reload to pick up env var
        import sml_beast.outreach.enricher as e
        import sml_beast.outreach.guardrails as g

        importlib.reload(g)
        importlib.reload(e)
        self.e = e

    def tearDown(self):
        os.environ.pop("BEAST_OUTPUT_ROOT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── email validation ─────────────────────────────────────────────────────────

class EmailValidationTests(_EnricherBase):
    def test_valid_business_email(self):
        self.assertTrue(self.e.is_valid_email("tim@example.com"))
        self.assertTrue(self.e.is_valid_email("ops+bb7@startup.io"))
        self.assertTrue(self.e.is_valid_email("first.last@company.co.uk"))

    def test_rejects_empty_and_none(self):
        self.assertFalse(self.e.is_valid_email(""))
        self.assertFalse(self.e.is_valid_email(None))

    def test_rejects_missing_at(self):
        self.assertFalse(self.e.is_valid_email("notanemail.com"))

    def test_rejects_missing_tld(self):
        self.assertFalse(self.e.is_valid_email("user@host"))

    def test_rejects_role_accounts(self):
        for role in ("abuse", "postmaster", "noreply", "no-reply", "support",
                     "info", "contact", "webmaster", "admin", "security",
                     "help", "hello", "privacy", "legal", "unsubscribe"):
            self.assertFalse(
                self.e.is_valid_email(f"{role}@example.com"),
                f"{role}@ should be rejected",
            )

    def test_case_insensitive_role_check(self):
        self.assertFalse(self.e.is_valid_email("Noreply@Example.COM"))


# ── security.txt parser ──────────────────────────────────────────────────────

class SecurityTxtParserTests(_EnricherBase):
    def test_mailto_form(self):
        txt = "Contact: mailto:dev@example.com\nExpires: 2099-01-01T00:00:00Z\n"
        email, lines = self.e._parse_security_txt(txt)
        self.assertEqual(email, "dev@example.com")
        self.assertIn("mailto:dev@example.com", lines)

    def test_bare_email_form(self):
        txt = "Contact: ops@example.com\n"
        email, _ = self.e._parse_security_txt(txt)
        self.assertEqual(email, "ops@example.com")

    def test_https_contact_skipped(self):
        txt = "Contact: https://example.com/report\n"
        email, _ = self.e._parse_security_txt(txt)
        self.assertIsNone(email)

    def test_comment_lines_ignored(self):
        txt = "# This is a comment\nContact: mailto:real@example.com\n"
        email, _ = self.e._parse_security_txt(txt)
        self.assertEqual(email, "real@example.com")

    def test_role_account_in_security_txt_rejected(self):
        txt = "Contact: mailto:security@example.com\n"
        email, _ = self.e._parse_security_txt(txt)
        self.assertIsNone(email)

    def test_first_valid_contact_wins(self):
        txt = (
            "Contact: https://example.com/report\n"
            "Contact: mailto:info@example.com\n"  # role — rejected
            "Contact: mailto:tim@example.com\n"   # valid
        )
        email, _ = self.e._parse_security_txt(txt)
        self.assertEqual(email, "tim@example.com")


# ── humans.txt parser ────────────────────────────────────────────────────────

class HumansTxtParserTests(_EnricherBase):
    def test_contact_line(self):
        txt = "/* TEAM */\n  Contact: alice@example.com\n"
        email, _ = self.e._parse_humans_txt(txt)
        self.assertEqual(email, "alice@example.com")

    def test_author_line_with_name_format(self):
        txt = "Author: Bob Smith <bob@example.com>\n"
        email, _ = self.e._parse_humans_txt(txt)
        self.assertEqual(email, "bob@example.com")

    def test_no_email_returns_none(self):
        txt = "Author: Anonymous\nContact: https://example.com\n"
        email, _ = self.e._parse_humans_txt(txt)
        self.assertIsNone(email)

    def test_role_account_rejected(self):
        txt = "Contact: info@example.com\n"
        email, _ = self.e._parse_humans_txt(txt)
        self.assertIsNone(email)


# ── humans.json parser ───────────────────────────────────────────────────────

class HumansJsonParserTests(_EnricherBase):
    def test_contacts_array(self):
        data = {"contacts": [{"name": "Alice", "email": "alice@example.com"}]}
        email, _ = self.e._parse_humans_json(json.dumps(data))
        self.assertEqual(email, "alice@example.com")

    def test_team_key_alias(self):
        data = {"team": [{"email": "bob@example.com"}]}
        email, _ = self.e._parse_humans_json(json.dumps(data))
        self.assertEqual(email, "bob@example.com")

    def test_root_list_form(self):
        data = [{"email": "charlie@example.com"}]
        email, _ = self.e._parse_humans_json(json.dumps(data))
        self.assertEqual(email, "charlie@example.com")

    def test_bad_json_returns_none(self):
        email, _ = self.e._parse_humans_json("{not valid json")
        self.assertIsNone(email)

    def test_role_account_in_json_rejected(self):
        data = {"contacts": [{"email": "admin@example.com"}]}
        email, _ = self.e._parse_humans_json(json.dumps(data))
        self.assertIsNone(email)

    def test_skips_contacts_without_email_key(self):
        data = {"contacts": [{"name": "Ghost"}, {"email": "real@example.com"}]}
        email, _ = self.e._parse_humans_json(json.dumps(data))
        self.assertEqual(email, "real@example.com")


# ── author page parser ───────────────────────────────────────────────────────

class AuthorPageParserTests(_EnricherBase):
    def test_mailto_href(self):
        html = '<a href="mailto:dev@example.com">Email us</a>'
        email, _ = self.e._parse_author_page(html)
        self.assertEqual(email, "dev@example.com")

    def test_mailto_with_query_stripped(self):
        html = '<a href="mailto:dev@example.com?subject=Hi">Contact</a>'
        email, _ = self.e._parse_author_page(html)
        self.assertEqual(email, "dev@example.com")

    def test_visible_text_fallback(self):
        html = "<p>Reach us at dev@example.com for questions.</p>"
        email, _ = self.e._parse_author_page(html)
        self.assertEqual(email, "dev@example.com")

    def test_role_account_rejected(self):
        html = '<a href="mailto:info@example.com">Info</a>'
        email, _ = self.e._parse_author_page(html)
        self.assertIsNone(email)

    def test_no_email_returns_none(self):
        html = "<p>No contact info here.</p>"
        email, _ = self.e._parse_author_page(html)
        self.assertIsNone(email)


# ── source priority waterfall ─────────────────────────────────────────────────

class SourcePriorityTests(_EnricherBase):
    """Mock _fetch to control which URLs return content."""

    def _make_fetch(self, url_map: dict):
        """Returns a mock _fetch that returns content keyed by URL substring."""
        def _mock_fetch(url, **kwargs):
            for fragment, content in url_map.items():
                if fragment in url:
                    return content
            return None
        return _mock_fetch

    def test_security_txt_wins_over_humans_txt(self):
        fetch_map = {
            "security.txt": "Contact: mailto:sec@example.com\n",
            "humans.txt": "Contact: humans@example.com\n",
        }
        with patch.object(self.e, "_fetch", side_effect=self._make_fetch(fetch_map)):
            r = self.e._enrich_live("example.com")
        self.assertEqual(r.email, "sec@example.com")
        self.assertEqual(r.source, "security.txt")

    def test_humans_txt_used_when_security_txt_absent(self):
        fetch_map = {
            "humans.txt": "Contact: humans@example.com\n",
        }
        with patch.object(self.e, "_fetch", side_effect=self._make_fetch(fetch_map)):
            r = self.e._enrich_live("example.com")
        self.assertEqual(r.email, "humans@example.com")
        self.assertEqual(r.source, "humans.txt")

    def test_contact_txt_used_when_above_absent(self):
        fetch_map = {
            "contact.txt": "Contact: ctxt@example.com\n",
        }
        with patch.object(self.e, "_fetch", side_effect=self._make_fetch(fetch_map)):
            r = self.e._enrich_live("example.com")
        self.assertEqual(r.email, "ctxt@example.com")
        self.assertEqual(r.source, "contact.txt")

    def test_humans_json_used_when_txt_sources_absent(self):
        data = {"contacts": [{"email": "jsonuser@example.com"}]}
        fetch_map = {
            "humans.json": json.dumps(data),
        }
        with patch.object(self.e, "_fetch", side_effect=self._make_fetch(fetch_map)):
            r = self.e._enrich_live("example.com")
        self.assertEqual(r.email, "jsonuser@example.com")
        self.assertEqual(r.source, "humans.json")

    def test_author_page_used_as_fallback(self):
        html = '<a href="mailto:dev@example.com">Contact</a>'
        fetch_map = {"/about": html}
        with patch.object(self.e, "_fetch", side_effect=self._make_fetch(fetch_map)):
            r = self.e._enrich_live("example.com")
        self.assertEqual(r.email, "dev@example.com")
        self.assertIn("author_page", r.source)

    def test_no_contact_returns_unenriched_result(self):
        with patch.object(self.e, "_fetch", return_value=None):
            r = self.e._enrich_live("dark.com")
        self.assertIsNone(r.email)
        self.assertFalse(r.enriched)


# ── cache layer ──────────────────────────────────────────────────────────────

class CacheTests(_EnricherBase):
    def _prefill_cache(self, domain: str, email: str, age_s: int = 0) -> None:
        """Write a cache entry with a custom fetched_at_utc."""
        result = self.e.EnrichmentResult(
            domain=domain,
            email=email,
            source="security.txt",
            fetched_at_utc=int(time.time()) - age_s,
        )
        self.e._save_cached(result)

    def test_cache_hit_within_ttl(self):
        self._prefill_cache("cached.com", "fresh@cached.com", age_s=0)
        with patch.object(self.e, "_enrich_live") as mock_live:
            r = self.e.enrich_domain("cached.com")
        mock_live.assert_not_called()
        self.assertEqual(r.email, "fresh@cached.com")

    def test_cache_miss_after_ttl(self):
        # 31 days old — expired
        old_s = 31 * 86400
        self._prefill_cache("stale.com", "old@stale.com", age_s=old_s)
        fresh = self.e.EnrichmentResult(domain="stale.com", email="new@stale.com", source="humans.txt")
        with patch.object(self.e, "_enrich_live", return_value=fresh):
            r = self.e.enrich_domain("stale.com")
        self.assertEqual(r.email, "new@stale.com")

    def test_force_refresh_bypasses_valid_cache(self):
        self._prefill_cache("fresh.com", "cached@fresh.com", age_s=0)
        live = self.e.EnrichmentResult(domain="fresh.com", email="live@fresh.com", source="humans.txt")
        with patch.object(self.e, "_enrich_live", return_value=live):
            r = self.e.enrich_domain("fresh.com", force_refresh=True)
        self.assertEqual(r.email, "live@fresh.com")

    def test_atomic_write_no_tmp_left_behind(self):
        self._prefill_cache("atomic.com", "dev@atomic.com")
        cache_path = self.e._cache_path("atomic.com")
        tmp_path = cache_path.with_suffix(".tmp")
        self.assertTrue(cache_path.exists())
        self.assertFalse(tmp_path.exists(), ".tmp must be renamed away after write")

    def test_cache_round_trip_preserves_all_fields(self):
        original = self.e.EnrichmentResult(
            domain="roundtrip.com",
            email="rt@roundtrip.com",
            source="security.txt",
            raw_contact_lines=["mailto:rt@roundtrip.com"],
        )
        self.e._save_cached(original)
        loaded = self.e._load_cached("roundtrip.com")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.email, "rt@roundtrip.com")
        self.assertEqual(loaded.source, "security.txt")
        self.assertEqual(loaded.raw_contact_lines, ["mailto:rt@roundtrip.com"])

    def test_corrupt_cache_file_returns_none(self):
        p = self.e._cache_path("corrupt.com")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{invalid json")
        result = self.e._load_cached("corrupt.com")
        self.assertIsNone(result)


# ── enrich_domain public API ─────────────────────────────────────────────────

class EnrichDomainPublicAPITests(_EnricherBase):
    def test_empty_domain_returns_error_result(self):
        r = self.e.enrich_domain("")
        self.assertFalse(r.enriched)
        self.assertIsNotNone(r.error)

    def test_network_exception_returns_error_result(self):
        with patch.object(self.e, "_enrich_live", side_effect=RuntimeError("network down")):
            r = self.e.enrich_domain("error.com")
        self.assertFalse(r.enriched)
        self.assertIsNotNone(r.error)
        self.assertIn("network down", r.error)

    def test_successful_enrich_returns_enriched_true(self):
        live = self.e.EnrichmentResult(
            domain="success.com", email="dev@success.com", source="security.txt"
        )
        with patch.object(self.e, "_enrich_live", return_value=live):
            r = self.e.enrich_domain("success.com")
        self.assertTrue(r.enriched)
        self.assertEqual(r.email, "dev@success.com")

    def test_domain_normalized_to_lowercase(self):
        """enrich_domain normalizes domain to lower before cache lookup."""
        live = self.e.EnrichmentResult(
            domain="example.com", email="dev@example.com", source="security.txt"
        )
        with patch.object(self.e, "_enrich_live", return_value=live) as mock_live:
            self.e.enrich_domain("EXAMPLE.COM")
        # _enrich_live must receive the lowercased domain
        call_domain = mock_live.call_args[0][0]
        self.assertEqual(call_domain, "example.com")

    def test_result_is_cached_after_live_fetch(self):
        live = self.e.EnrichmentResult(
            domain="newdomain.com", email="dev@newdomain.com", source="humans.txt"
        )
        with patch.object(self.e, "_enrich_live", return_value=live):
            self.e.enrich_domain("newdomain.com")
        # Cache file should exist now
        self.assertTrue(self.e._cache_path("newdomain.com").exists())


if __name__ == "__main__":
    unittest.main()
