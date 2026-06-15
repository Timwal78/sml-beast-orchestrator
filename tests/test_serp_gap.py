"""SERP-Gap engine tests. Synthetic SERPs shaped exactly like Serper.dev
normalized payloads — no network."""

import os
import shutil
import tempfile
import unittest

from sml_beast.intel.serp_gap import analyze, synthesize_page_brief


def serp(query: str, organic: list, paa: list | None = None, related: list | None = None) -> dict:
    return {
        "query": query,
        "organic": organic,
        "people_also_ask": paa or [],
        "related": [{"query": r} for r in (related or [])],
    }


def r(title: str, link: str, snippet: str = "") -> dict:
    return {"title": title, "link": link, "snippet": snippet}


BRAND = ("scriptmasterlabs.com",)


class GapEngineTests(unittest.TestCase):
    def test_brand_present_top3_is_low_severity(self):
        rep = analyze(
            serp(
                "mastersheets",
                [
                    r("MasterSheets", "https://www.scriptmasterlabs.com/mastersheets"),
                    r("Capterra", "https://capterra.com/foo"),
                    r("Reddit", "https://reddit.com/r/spreadsheets/foo"),
                ],
            ),
            brand_domains=BRAND,
        )
        self.assertEqual(rep.gap_severity, "LOW")
        self.assertTrue(rep.brand_present)
        self.assertEqual(rep.brand_positions, [1])

    def test_two_aggregators_is_critical(self):
        rep = analyze(
            serp(
                "google sheets alternative",
                [
                    r("10 Best Google Sheets Alternatives", "https://capterra.com/alts"),
                    r("Google Sheets vs alternatives", "https://g2.com/google-sheets"),
                    r("Microsoft Excel", "https://www.microsoft.com/excel"),
                ],
            ),
            brand_domains=BRAND,
        )
        self.assertEqual(rep.gap_severity, "CRITICAL")
        self.assertFalse(rep.brand_present)
        self.assertIn("aggregator", rep.top3_classes)

    def test_two_entrenched_is_low_severity(self):
        rep = analyze(
            serp(
                "what is xrpl",
                [
                    r("XRPL — Wikipedia", "https://en.wikipedia.org/wiki/XRP_Ledger"),
                    r("XRPL on Microsoft Learn", "https://support.microsoft.com/xrpl"),
                    r("Independent post", "https://example.com/xrpl"),
                ],
            ),
            brand_domains=BRAND,
        )
        # 2 entrenched in top3 -> incumbents own this; not worth the page burn
        self.assertEqual(rep.gap_severity, "LOW")
        self.assertEqual(rep.top3_classes.count("entrenched"), 2)

    def test_forum_listicle_mix_is_high_or_critical(self):
        rep = analyze(
            serp(
                "x402 payment protocol",
                [
                    r("x402 — explained on Reddit", "https://reddit.com/r/web3/x402"),
                    r("Top 5 agent payment rails", "https://blog.example.com/top-5-agent-rails"),
                    r("Independent technical writeup", "https://eng.example.io/x402"),
                ],
            ),
            brand_domains=BRAND,
        )
        self.assertIn(rep.gap_severity, ("HIGH", "CRITICAL"))

    def test_priority_score_in_range(self):
        rep = analyze(
            serp(
                "byok ai spreadsheet",
                [
                    r("Spreadsheet roundup", "https://g2.com/foo"),
                    r("Reddit thread", "https://reddit.com/r/foo"),
                    r("Indie blog", "https://example.com/foo"),
                ],
                paa=[
                    {"question": "What is BYOK?", "snippet": "Bring Your Own Key…"},
                    {"question": "How does BYOK work in spreadsheets?", "snippet": "…"},
                ],
                related=[
                    "bring your own key llm",
                    "private ai spreadsheet",
                    "openai api key spreadsheet",
                ],
            ),
            brand_domains=BRAND,
        )
        self.assertGreaterEqual(rep.priority_score, 50)
        self.assertLessEqual(rep.priority_score, 100)

    def test_intent_detection_from_paa(self):
        rep = analyze(
            serp(
                "google sheets alternative",
                [r("x", "https://example.com")],
                paa=[{"question": "What is the best Google Sheets alternative?", "snippet": "…"}],
            ),
            brand_domains=BRAND,
        )
        self.assertIn("comparison", rep.recommended_intents)

    def test_synthesize_page_brief_preserves_canonical_facts(self):
        from sml_beast.content.briefs import MASTERSHEETS

        rep = analyze(
            serp(
                "mastersheets",
                [
                    r("X", "https://capterra.com/x"),
                    r("Y", "https://g2.com/y"),
                    r("Z", "https://reddit.com/z"),
                ],
                paa=[{"question": "Is MasterSheets free?", "snippet": "One-time payment."}],
            ),
            brand_domains=BRAND,
        )
        page_brief = synthesize_page_brief(MASTERSHEETS, rep)
        # canonical facts must survive untouched
        self.assertEqual(page_brief["name"], MASTERSHEETS["name"])
        self.assertFalse(page_brief["pricing"]["subscriptions"])
        # gap overlay attached
        self.assertIn("_gap", page_brief)
        self.assertEqual(page_brief["_gap"]["severity"], "CRITICAL")
        self.assertTrue(page_brief["_gap"]["paa"])


class AttackAngleTests(unittest.TestCase):
    def test_mastersheets_detects_subscription_fatigue(self):
        rep = analyze(
            serp(
                "google sheets alternative",
                [
                    r(
                        "Google Sheets — subscription pricing",
                        "https://workspace.google.com/sheets",
                        "Per month or annual plan",
                    ),
                    r(
                        "Best paid spreadsheet",
                        "https://capterra.com/x",
                        "Monthly fee starting at $12/mo",
                    ),
                    r("Compare plans", "https://example.com/y", "Annual subscription required"),
                ],
            ),
            brand_domains=BRAND,
            vertical="mastersheets",
        )
        codes = [a["code"] for a in rep.attack_angles]
        self.assertIn("exploit_subscription_fatigue", codes)
        # trigger count should reflect multiple hits
        sub = next(a for a in rep.attack_angles if a["code"] == "exploit_subscription_fatigue")
        self.assertGreaterEqual(sub["trigger_count"], 2)

    def test_mastersheets_privacy_gap_when_top3_silent(self):
        rep = analyze(
            serp(
                "collaborative spreadsheet",
                [
                    r("Top spreadsheet tools", "https://capterra.com/a", "Real-time editing"),
                    r("Spreadsheet roundup", "https://g2.com/b", "Charts and pivot tables"),
                    r("Best in 2025", "https://example.com/c", "Modern UI and fast"),
                ],
            ),
            brand_domains=BRAND,
            vertical="mastersheets",
        )
        codes = [a["code"] for a in rep.attack_angles]
        self.assertIn("exploit_privacy_gap", codes)

    def test_xrpl_x402_detects_api_friction_and_latency(self):
        rep = analyze(
            serp(
                "agent payment api",
                [
                    r(
                        "Stripe API for agents",
                        "https://stripe.com/x",
                        "Requires API key and rate limit",
                    ),
                    r(
                        "Slow settlement on legacy rails",
                        "https://example.com/y",
                        "Takes 3 minutes per confirmation",
                    ),
                    r("KYC required", "https://example.com/z", "Corporate account and credit line"),
                ],
            ),
            brand_domains=BRAND,
            vertical="xrpl_x402",
        )
        codes = [a["code"] for a in rep.attack_angles]
        self.assertIn("exploit_api_friction", codes)
        self.assertIn("exploit_latency", codes)
        self.assertIn("exploit_onboarding", codes)
        self.assertIn("position_vs_legacy_rails", codes)

    def test_no_vertical_returns_fallback(self):
        rep = analyze(serp("foo", [r("Bar", "https://example.com")]), brand_domains=BRAND)
        self.assertEqual(len(rep.attack_angles), 1)
        self.assertEqual(rep.attack_angles[0]["code"], "direct_structural_superiority")

    def test_unknown_vertical_returns_fallback(self):
        rep = analyze(
            serp("foo", [r("Bar", "https://example.com")]),
            brand_domains=BRAND,
            vertical="nonexistent",
        )
        self.assertEqual(rep.attack_angles[0]["code"], "direct_structural_superiority")

    def test_attack_angles_sorted_by_trigger_count(self):
        rep = analyze(
            serp(
                "alts",
                [
                    r("Subscription required", "https://x.com", "$5/month"),
                    r("Subscription pricing", "https://y.com", "annual plan"),
                    r("ChatGPT plugin", "https://z.com", "OpenAI integration"),
                ],
            ),
            brand_domains=BRAND,
            vertical="mastersheets",
        )
        # subscription rule hits twice, ai_lockin rule hits once -> subscription first
        codes = [a["code"] for a in rep.attack_angles]
        if "exploit_subscription_fatigue" in codes and "exploit_ai_lockin" in codes:
            self.assertLess(
                codes.index("exploit_subscription_fatigue"), codes.index("exploit_ai_lockin")
            )


class JsonLdFactoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beast-jsonld-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build(self, vertical: str, brief_dict: dict):
        from sml_beast.content.generator import write_page

        rep = analyze(
            serp(
                "test keyword",
                [
                    r("Capterra", "https://capterra.com/x"),
                    r("G2", "https://g2.com/y"),
                    r("Reddit", "https://reddit.com/z"),
                ],
                paa=[{"question": "Is it free?", "snippet": "One-time payment."}],
            ),
            brand_domains=BRAND,
            vertical=vertical,
        )
        page_brief = synthesize_page_brief(brief_dict, rep, vertical=vertical)
        path = write_page(self.tmp, page_brief, "test", "test keyword")
        with open(os.path.join(path, "schema.jsonld")) as f:
            import json as _j

            return _j.load(f)

    def test_mastersheets_emits_softwareapplication(self):
        from sml_beast.content.briefs import MASTERSHEETS

        blocks = self._build("mastersheets", MASTERSHEETS)
        types = [b.get("@type") for b in blocks]
        self.assertIn("Product", types)
        self.assertIn("SoftwareApplication", types)
        self.assertIn("FAQPage", types)
        sa = next(b for b in blocks if b["@type"] == "SoftwareApplication")
        self.assertEqual(sa["applicationCategory"], "BusinessApplication")
        self.assertEqual(sa["applicationSubCategory"], "Spreadsheet")
        self.assertIn("100% one-time payment — zero subscriptions", sa["featureList"])

    def test_xrpl_emits_organization_and_techarticle(self):
        from sml_beast.content.briefs import XRPL_X402

        blocks = self._build("xrpl_x402", XRPL_X402)
        types = [b.get("@type") for b in blocks]
        self.assertIn("Product", types)
        self.assertIn("Organization", types)
        self.assertIn("TechArticle", types)
        self.assertIn("FAQPage", types)
        org = next(b for b in blocks if b["@type"] == "Organization")
        self.assertEqual(org["name"], "ScriptMasterLabs")
        self.assertIn("https://www.scriptmasterlabs.com", org["sameAs"])
        self.assertIn("https://squeezeos-api.onrender.com", org["sameAs"])
        tech = next(b for b in blocks if b["@type"] == "TechArticle")
        self.assertEqual(tech["proficiencyLevel"], "Expert")
        self.assertIn("XRPL", tech["keywords"])
        self.assertIn("x402", tech["keywords"])


class GeneratorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beast-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_page_emits_mdx_and_jsonld(self):
        from sml_beast.content.briefs import MASTERSHEETS
        from sml_beast.content.generator import write_page

        rep = analyze(
            serp(
                "google sheets alternative",
                [
                    r("Capterra roundup", "https://capterra.com/x"),
                    r("G2 listicle", "https://g2.com/y"),
                    r("Reddit thread", "https://reddit.com/z"),
                ],
                paa=[{"question": "What is the best alternative?", "snippet": "MasterSheets."}],
                related=["one-time spreadsheet", "self-hosted spreadsheet"],
            ),
            brand_domains=BRAND,
            vertical="mastersheets",
        )
        page_brief = synthesize_page_brief(MASTERSHEETS, rep, vertical="mastersheets")
        out = write_page(self.tmp, page_brief, "alternative", "google sheets alternative")
        self.assertTrue(os.path.isfile(os.path.join(out, "page.mdx")))
        self.assertTrue(os.path.isfile(os.path.join(out, "schema.jsonld")))
        with open(os.path.join(out, "page.mdx")) as f:
            mdx = f.read()
        self.assertIn("needs_human_review: true", mdx)
        self.assertIn("priority_score:", mdx)
        self.assertIn("gap_severity:", mdx)
        # PAA promoted to body section
        self.assertIn("What is the best alternative?", mdx)
        # Operator landscape comment present
        self.assertIn("COMPETITIVE LANDSCAPE", mdx)
        # JSON-LD has both Product and FAQPage
        import json as _j

        with open(os.path.join(out, "schema.jsonld")) as f:
            blocks = _j.load(f)
        types = [b.get("@type") for b in blocks]
        self.assertIn("Product", types)
        self.assertIn("FAQPage", types)


if __name__ == "__main__":
    unittest.main()
