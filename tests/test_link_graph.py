"""Tests for the internal-link graph + backlink-target finder.

Together they form the offensive layer: link_graph bleeds authority across
the two product silos; backlink_targets harvests external placement targets
from the live SERPs already pulled for gap analysis."""

import json
import os
import shutil
import tempfile
import unittest

from sml_beast.content.link_graph import all_outbound_targets, render_cross_link_block
from sml_beast.intel.backlink_targets import BacklinkTargetFinder

# ── helpers ───────────────────────────────────────────────────────────────────


def r(title, link, snippet=""):
    return {"title": title, "link": link, "snippet": snippet}


def serp(organic):
    return {"organic": organic}


# ── link graph ────────────────────────────────────────────────────────────────


class LinkGraphTests(unittest.TestCase):
    def test_mastersheets_block_renders_with_x402_anchor(self):
        block = render_cross_link_block("mastersheets")
        self.assertTrue(block, "mastersheets vertical must produce a block")
        joined = "\n".join(block)
        # heading
        self.assertIn("## Inside the SML stack", joined)
        # cross-silo anchor text + target
        self.assertIn("sub-50ms x402 payment rails", joined)
        self.assertIn("scriptmasterlabs.com/infrastructure/x402", joined)
        # secondary anchor
        self.assertIn("SML institutional infrastructure", joined)

    def test_xrpl_block_renders_with_mastersheets_anchor(self):
        block = render_cross_link_block("xrpl_x402")
        self.assertTrue(block)
        joined = "\n".join(block)
        self.assertIn("## Front-end nodes on the SML stack", joined)
        # cross-silo anchor text + target
        self.assertIn("institutional-grade utility applications like MasterSheets", joined)
        self.assertIn("scriptmasterlabs.com/mastersheets", joined)
        # secondary anchor
        self.assertIn("SML application catalog", joined)

    def test_unknown_vertical_produces_empty_block(self):
        # No fabricated links for verticals without a registered cross-link.
        self.assertEqual(render_cross_link_block("nonexistent"), [])
        self.assertEqual(render_cross_link_block(None), [])

    def test_outbound_targets_match_block_links(self):
        ms_targets = all_outbound_targets("mastersheets")
        xr_targets = all_outbound_targets("xrpl_x402")
        self.assertEqual(len(ms_targets), 2)
        self.assertEqual(len(xr_targets), 2)
        # both verticals must point at scriptmasterlabs.com (cross-silo, same brand)
        for u in ms_targets + xr_targets:
            self.assertIn("scriptmasterlabs.com", u)

    def test_block_appears_in_generated_page_body(self):
        """Integration: render_cross_link_block output lands in the MDX body."""
        from sml_beast.content.briefs import MASTERSHEETS
        from sml_beast.content.generator import write_page
        from sml_beast.intel.serp_gap import analyze, synthesize_page_brief

        rep = analyze(
            {
                "organic": [
                    r("Capterra roundup", "https://capterra.com/x"),
                    r("G2 listicle", "https://g2.com/y"),
                    r("Random review", "https://example.com/z"),
                ]
            },
            brand_domains=("scriptmasterlabs.com",),
            vertical="mastersheets",
        )
        page_brief = synthesize_page_brief(MASTERSHEETS, rep, vertical="mastersheets")

        tmp = tempfile.mkdtemp(prefix="beast-link-")
        try:
            out = write_page(tmp, page_brief, "alternative", "google sheets alternative")
            with open(os.path.join(out, "page.mdx")) as f:
                mdx = f.read()
            self.assertIn("Inside the SML stack", mdx)
            self.assertIn("sub-50ms x402 payment rails", mdx)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ── backlink-target finder ────────────────────────────────────────────────────


class BacklinkFinderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beast-bounty-")
        self.finder = BacklinkTargetFinder(brand_domains=("scriptmasterlabs.com",))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_filters_brand_megasites_entrenched_forums(self):
        self.finder.ingest(
            serp(
                [
                    r("Capterra: top spreadsheets", "https://capterra.com/spreadsheets/"),
                    r("My own page", "https://www.scriptmasterlabs.com/mastersheets"),
                    r("Wikipedia: spreadsheet", "https://en.wikipedia.org/wiki/Spreadsheet"),
                    r("Reddit r/spreadsheets", "https://reddit.com/r/spreadsheets"),
                    r("YouTube tutorial", "https://youtube.com/watch?v=abc"),
                    r("Niche SaaS review", "https://saasblog.example.com/spreadsheet-review"),
                ]
            ),
            keyword="google sheets alternative",
        )

        domains = {t["domain"] for t in self.finder.ranked()}
        # only aggregator + neutral survive
        self.assertIn("capterra.com", domains)
        self.assertIn("saasblog.example.com", domains)
        # brand
        self.assertNotIn("scriptmasterlabs.com", domains)
        # entrenched
        self.assertNotIn("en.wikipedia.org", domains)
        self.assertNotIn("wikipedia.org", domains)
        # forum
        self.assertNotIn("reddit.com", domains)
        # megasite
        self.assertNotIn("youtube.com", domains)

    def test_scoring_priorities_aggregator_over_neutral(self):
        # capterra appears 2x (aggregator, weight 4), saasblog appears 3x
        # (neutral, weight 2). capterra: 2*4=8, saasblog: 3*2=6 -> capterra wins.
        for kw in ("kw1", "kw2"):
            self.finder.ingest(
                serp(
                    [
                        r("Capterra A", "https://capterra.com/a"),
                        r("SaaS Blog", "https://saasblog.example.com/a"),
                    ]
                ),
                keyword=kw,
            )
        self.finder.ingest(
            serp(
                [
                    r("SaaS Blog 2", "https://saasblog.example.com/b"),
                ]
            ),
            keyword="kw3",
        )

        ranked = self.finder.ranked()
        domains_in_order = [t["domain"] for t in ranked]
        self.assertEqual(domains_in_order[0], "capterra.com")
        capterra = ranked[0]
        self.assertEqual(capterra["class"], "aggregator")
        self.assertEqual(capterra["class_weight"], 4)
        self.assertEqual(capterra["frequency"], 2)
        self.assertEqual(capterra["priority_score"], 8)

    def test_listicle_classified_when_title_matches(self):
        self.finder.ingest(
            serp(
                [
                    r("10 best spreadsheet alternatives in 2025", "https://techblog.example.com/x"),
                ]
            ),
            keyword="alternatives",
        )
        ranked = self.finder.ranked()
        self.assertEqual(ranked[0]["class"], "listicle")
        self.assertEqual(ranked[0]["class_weight"], 3)

    def test_discovered_via_dedupes_and_caps(self):
        # 6 distinct keywords -> SAMPLE_CAP (5) entries kept; duplicates ignored.
        for kw in ("a", "b", "c", "d", "e", "f", "a"):
            self.finder.ingest(
                serp(
                    [
                        r("Capterra", "https://capterra.com/x"),
                    ]
                ),
                keyword=kw,
            )
        ranked = self.finder.ranked()
        self.assertEqual(ranked[0]["domain"], "capterra.com")
        self.assertLessEqual(len(ranked[0]["discovered_via"]), 5)
        # frequency counts every appearance (7 ingestions = 7)
        self.assertEqual(ranked[0]["frequency"], 7)

    def test_flush_writes_json_and_is_idempotent(self):
        self.finder.ingest(
            serp(
                [
                    r("Capterra", "https://capterra.com/x"),
                    r("G2", "https://g2.com/y"),
                ]
            ),
            keyword="alts",
        )
        path1 = self.finder.flush("mastersheets", self.tmp)
        path2 = self.finder.flush("mastersheets", self.tmp)  # second flush, same data
        self.assertEqual(path1, path2)
        self.assertTrue(os.path.isfile(path1))
        with open(path1) as f:
            payload = json.load(f)
        self.assertEqual(payload["vertical"], "mastersheets")
        self.assertEqual(payload["total_serps_ingested"], 1)
        self.assertEqual(payload["total_domains"], 2)
        domains = {t["domain"] for t in payload["targets"]}
        self.assertEqual(domains, {"capterra.com", "g2.com"})

    def test_ranked_output_is_stable_for_ties(self):
        # Two domains with identical class + frequency -> alphabetical tiebreak
        for _ in range(2):
            self.finder.ingest(
                serp(
                    [
                        r("Capterra", "https://capterra.com/x"),
                        r("G2", "https://g2.com/y"),
                    ]
                ),
                keyword="alts",
            )
        ranked = self.finder.ranked()
        self.assertEqual(ranked[0]["domain"], "capterra.com")
        self.assertEqual(ranked[1]["domain"], "g2.com")


if __name__ == "__main__":
    unittest.main()
