"""Tests for sml_beast/outreach/templates.py — pitch template generation.

Covers:
  - Required-field enforcement: every missing or empty field raises
  - Vertical validation: unknown vertical raises ValueError
  - Subject line: both verticals, site_name interpolated
  - Body rendering: all placeholders filled for each vertical
  - observation_for_attack_angles: known angle, unknown angle (default),
    empty list (default), multiple angles (first match wins)
  - PitchEmail.to_dict() round-trips correctly
"""

import unittest

from sml_beast.outreach.templates import (
    PitchEmail,
    TemplateMissingVariableError,
    observation_for_attack_angles,
    render_pitch,
    subject_for_vertical,
)

# ── minimal valid context factory ────────────────────────────────────────────

_BASE_CTX = {
    "first_name_or_team_handle": "Team",
    "pers_observation": "I noticed your content.",
    "pers_gap_finding": "You have a gap.",
    "usdc_amount": "5.00",
    "enrichment_source": "security.txt",
    "xrpl_tx_hash": "ABC123DEADBEEF",
    "settlement_time_ms": "47",
    "anchor_url": "https://scriptmasterlabs.com/mastersheets",
    "anchor_resource_title": "MasterSheets",
    "pers_target_url": "https://example.com/spreadsheet-tools",
    "opt_out_url": "https://scriptmasterlabs.com/optout",
    "operator_signature": "Timothy",
    "domain": "example.com",
    "site_name": "Example Blog",
}


def _ctx(**overrides):
    c = dict(_BASE_CTX)
    c.update(overrides)
    return c


# ── required field enforcement ────────────────────────────────────────────────

class RequiredFieldTests(unittest.TestCase):
    def test_all_required_fields_present_succeeds(self):
        result = render_pitch("mastersheets", _ctx())
        self.assertIsInstance(result, PitchEmail)

    def test_missing_field_raises(self):
        ctx = _ctx()
        del ctx["xrpl_tx_hash"]
        with self.assertRaises(TemplateMissingVariableError) as cm:
            render_pitch("mastersheets", ctx)
        self.assertIn("xrpl_tx_hash", str(cm.exception))

    def test_empty_string_field_raises(self):
        with self.assertRaises(TemplateMissingVariableError):
            render_pitch("mastersheets", _ctx(xrpl_tx_hash=""))

    def test_none_field_raises(self):
        with self.assertRaises(TemplateMissingVariableError):
            render_pitch("mastersheets", _ctx(anchor_url=None))

    def test_each_required_field_enforced(self):
        """Every field in _REQUIRED_FIELDS triggers an error when absent."""
        from sml_beast.outreach.templates import _REQUIRED_FIELDS

        for field in _REQUIRED_FIELDS:
            ctx = _ctx()
            del ctx[field]
            with self.assertRaises(TemplateMissingVariableError, msg=f"{field} not enforced"):
                render_pitch("mastersheets", ctx)


# ── vertical validation ───────────────────────────────────────────────────────

class VerticalValidationTests(unittest.TestCase):
    def test_unknown_vertical_raises(self):
        with self.assertRaises(ValueError) as cm:
            render_pitch("made_up_vertical", _ctx())
        self.assertIn("made_up_vertical", str(cm.exception))

    def test_mastersheets_accepted(self):
        result = render_pitch("mastersheets", _ctx())
        self.assertEqual(result.vertical, "mastersheets")

    def test_xrpl_x402_accepted(self):
        result = render_pitch("xrpl_x402", _ctx())
        self.assertEqual(result.vertical, "xrpl_x402")


# ── subject line ──────────────────────────────────────────────────────────────

class SubjectLineTests(unittest.TestCase):
    def test_mastersheets_subject_contains_site_name(self):
        subj = subject_for_vertical("mastersheets", "AcmeBlog")
        self.assertIn("AcmeBlog", subj)

    def test_xrpl_x402_subject_contains_site_name(self):
        subj = subject_for_vertical("xrpl_x402", "CryptoCrunch")
        self.assertIn("CryptoCrunch", subj)

    def test_unknown_vertical_raises(self):
        with self.assertRaises(ValueError):
            subject_for_vertical("bogus", "SomeSite")

    def test_render_pitch_subject_matches_standalone(self):
        result = render_pitch("mastersheets", _ctx(site_name="MyBlog"))
        expected = subject_for_vertical("mastersheets", "MyBlog")
        self.assertEqual(result.subject, expected)


# ── body rendering ────────────────────────────────────────────────────────────

class BodyRenderingTests(unittest.TestCase):
    def test_tx_hash_appears_in_body(self):
        result = render_pitch("mastersheets", _ctx(xrpl_tx_hash="TXHASH999"))
        self.assertIn("TXHASH999", result.body)

    def test_usdc_amount_appears_in_body(self):
        result = render_pitch("mastersheets", _ctx(usdc_amount="10.00"))
        self.assertIn("10.00", result.body)

    def test_opt_out_url_in_body(self):
        result = render_pitch("xrpl_x402", _ctx(opt_out_url="https://sml.com/stop"))
        self.assertIn("https://sml.com/stop", result.body)

    def test_anchor_resource_title_in_body(self):
        result = render_pitch("mastersheets", _ctx(anchor_resource_title="MasterSheets Pro"))
        self.assertIn("MasterSheets Pro", result.body)

    def test_first_name_in_body(self):
        result = render_pitch("mastersheets", _ctx(first_name_or_team_handle="Alice"))
        self.assertIn("Hi Alice,", result.body)

    def test_no_unfilled_placeholders(self):
        result = render_pitch("mastersheets", _ctx())
        # No Python-style {word} placeholders left in the body
        import re
        leftovers = re.findall(r"\{[a-z_]+\}", result.body)
        self.assertEqual(leftovers, [], f"Unfilled placeholders: {leftovers}")

    def test_domain_set_on_result(self):
        result = render_pitch("mastersheets", _ctx(domain="target.com"))
        self.assertEqual(result.domain, "target.com")


# ── observation builder ───────────────────────────────────────────────────────

class ObservationBuilderTests(unittest.TestCase):
    def test_known_mastersheets_angle(self):
        obs = observation_for_attack_angles(["data_sovereignty"], "mastersheets")
        self.assertIn("data sovereignty", obs.lower())

    def test_known_x402_angle(self):
        obs = observation_for_attack_angles(["coinbase_facilitator"], "xrpl_x402")
        self.assertIn("Coinbase", obs)

    def test_unknown_angle_returns_default(self):
        obs = observation_for_attack_angles(["completely_unknown_angle"], "mastersheets")
        self.assertIsNotNone(obs)
        self.assertGreater(len(obs), 10)

    def test_empty_angles_returns_default(self):
        obs = observation_for_attack_angles([], "xrpl_x402")
        self.assertIsNotNone(obs)
        self.assertGreater(len(obs), 10)

    def test_first_matching_angle_wins(self):
        angles = ["completely_bogus", "pricing_model", "byok_ai"]
        obs = observation_for_attack_angles(angles, "mastersheets")
        # "pricing_model" is the first valid one — should win
        self.assertIn("subscription", obs.lower())

    def test_unknown_vertical_returns_default_gracefully(self):
        obs = observation_for_attack_angles(["any_angle"], "unknown_vertical")
        self.assertIsNotNone(obs)

    def test_returns_non_empty_string_always(self):
        for vertical in ("mastersheets", "xrpl_x402"):
            obs = observation_for_attack_angles([], vertical)
            self.assertTrue(obs.strip(), f"Empty obs for {vertical}")


# ── PitchEmail dataclass ──────────────────────────────────────────────────────

class PitchEmailDataclassTests(unittest.TestCase):
    def test_to_dict_includes_all_keys(self):
        email = PitchEmail(
            subject="Sub", body="Body", vertical="mastersheets", domain="ex.com"
        )
        d = email.to_dict()
        self.assertEqual(set(d.keys()), {"subject", "body", "vertical", "domain"})

    def test_to_dict_values_match_fields(self):
        email = PitchEmail(
            subject="S", body="B", vertical="xrpl_x402", domain="d.com"
        )
        d = email.to_dict()
        self.assertEqual(d["subject"], "S")
        self.assertEqual(d["body"], "B")
        self.assertEqual(d["vertical"], "xrpl_x402")
        self.assertEqual(d["domain"], "d.com")


if __name__ == "__main__":
    unittest.main()
