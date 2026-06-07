"""End-to-end smoke test — exercises the full BB7 pipeline in one shot.

The per-module unit tests verify each component in isolation. This file
proves that the components actually wire together. If a refactor breaks
the integration surface, this test fails first.

Pipeline exercised:

  1. bounty_targets.json staged in tempdir
  2. Manual review gate pre-cleared
  3. enrich_domain() mocked → returns a contact
  4. balance_check_fn injected → returns healthy
  5. xrpl_client mocked → returns a tx hash
  6. send_pitch mocked → returns a Message-ID
  7. agent.run_cycle() processes the target end-to-end:
       atomic_reserve_pitch → XRPL → state transition → SMTP → state
       transition → thread map update → verification pass → alerts
  8. Domain is now PITCH_DELIVERED with the tx hash recorded
  9. Thread map persisted Message-ID → domain
 10. opctl status emits a JSON snapshot with the expected counts
 11. Simulated inbound STOP reply via reply_monitor mock IMAP
 12. After reply: domain is OPTED_OUT
 13. opctl recent shows OPTED_OUT as the latest event

No real XRPL. No real SMTP. No real IMAP. No real HTTP. Every external
boundary is injected.
"""

import importlib
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

_VALID_SEED = "sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r"


def _ok_discord_response():
    r = MagicMock()
    r.status_code = 204
    return r


def _make_inbound_reply_email(in_reply_to: str, body: str) -> bytes:
    """Build a minimal raw RFC-2822 email as IMAP would return it."""
    lines = [
        "From: dev@target.com",
        "To: outreach@infra.scriptmasterlabs.com",
        "Subject: Re: BB7 outreach pitch",
        f"In-Reply-To: {in_reply_to}",
        "",
        body,
    ]
    return "\r\n".join(lines).encode("utf-8")


def _build_mock_imap(messages: list[tuple[str, bytes]]):
    imap = MagicMock()
    imap.login.return_value = ("OK", [])
    imap.select.return_value = ("OK", [])
    uids = [m[0].encode() for m in messages]
    imap.search.return_value = ("OK", [b" ".join(uids) if uids else b""])

    def fake_fetch(uid, parts):
        for u, raw in messages:
            if u.encode() == uid:
                return ("OK", [(b"header", raw)])
        return ("NO", [])

    imap.fetch.side_effect = fake_fetch
    imap.store.return_value = ("OK", [])
    imap.logout.return_value = ("BYE", [])
    return imap


class FullPipelineSmokeTest(unittest.TestCase):
    """End-to-end test of the full BB7 pipeline.

    Anything that breaks the integration surface between modules will
    fail this test before reaching the smaller unit suites."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beast-e2e-")
        os.environ["BEAST_OUTPUT_ROOT"] = self.tmp
        os.environ["BB7_XRPL_WALLET_SEED"] = _VALID_SEED
        os.environ["BB7_XRPL_NETWORK"] = "testnet"
        os.environ["BB7_SMTP_HOST"] = "smtp.example.com"
        os.environ["BB7_SMTP_USER"] = "outreach@infra.sml.com"
        os.environ["BB7_SMTP_PASS"] = "s3cr3t"
        os.environ["BB7_OPT_OUT_URL"] = "https://sml.com/opt"
        os.environ["BB7_OPERATOR_ADDRESS"] = "ScriptMasterLabs"
        os.environ["BB7_OPERATOR_SIGNATURE"] = "Tim"
        os.environ["BB7_IMAP_HOST"] = "imap.example.com"
        os.environ["BB7_IMAP_USER"] = "outreach@infra.sml.com"
        os.environ["BB7_IMAP_PASS"] = "s3cr3t"
        os.environ.pop("BB7_OUTREACH_DRY_RUN", None)
        os.environ.pop("BB7_DISCORD_ALERT_WEBHOOK", None)

        # Reload every module so env-derived paths point at the tmpdir
        import sml_beast.outreach.agent as a
        import sml_beast.outreach.alerts as al
        import sml_beast.outreach.balance as b
        import sml_beast.outreach.guardrails as g
        import sml_beast.outreach.opctl as o
        import sml_beast.outreach.reply_monitor as rm
        import sml_beast.outreach.state as s

        for mod in (g, s, al, b, rm, a, o):
            importlib.reload(mod)

        self.a = a
        self.s = s
        self.al = al
        self.b = b
        self.rm = rm
        self.o = o
        self.g = g
        self.root = Path(self.tmp)

    def tearDown(self):
        for k in (
            "BEAST_OUTPUT_ROOT", "BB7_XRPL_WALLET_SEED", "BB7_XRPL_NETWORK",
            "BB7_SMTP_HOST", "BB7_SMTP_USER", "BB7_SMTP_PASS",
            "BB7_OPT_OUT_URL", "BB7_OPERATOR_ADDRESS", "BB7_OPERATOR_SIGNATURE",
            "BB7_IMAP_HOST", "BB7_IMAP_USER", "BB7_IMAP_PASS",
            "BB7_OUTREACH_DRY_RUN", "BB7_DISCORD_ALERT_WEBHOOK",
        ):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stage_bounty(self, vertical: str, domain: str) -> None:
        d = self.root / vertical
        d.mkdir(parents=True, exist_ok=True)
        targets = [
            {
                "domain": domain,
                "priority_score": 9,
                "class": "listicle",
                "attack_angles": ["data_sovereignty"],
                "top_keyword": "best spreadsheet tools",
            }
        ]
        with open(d / "bounty_targets.json", "w") as f:
            json.dump({"targets": targets, "vertical": vertical}, f)

    def _clear_review_gate(self, vertical: str) -> None:
        sm = self.s.OutreachStateMachine()
        for _ in range(self.s.OUTREACH_MANUAL_REVIEW_N):
            sm.record_manual_review_completed(vertical)

    def _healthy_balance_fn(self):
        check = MagicMock()
        check.healthy = True
        check.error = None
        check.usdc_equiv = 50.0
        check.to_dict.return_value = {"healthy": True, "usdc_equiv": 50.0, "error": None}
        return lambda: check

    def _opctl_run(self, *argv) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self.o.main(list(argv))
        return code, buf.getvalue()

    def test_full_outbound_to_inbound_pipeline(self):
        # ── PART 1: OUTBOUND ──
        target_domain = "target.com"
        vertical = "mastersheets"

        self._stage_bounty(vertical, target_domain)
        self._clear_review_gate(vertical)

        # Mock enrichment to return a synthetic contact
        enriched = MagicMock()
        enriched.enriched = True
        enriched.email = "dev@target.com"
        enriched.source = "security.txt"

        # Mock XRPL client to return a tx hash
        xrpl_client = MagicMock()
        xrpl_client.send_demo_payment_for_usdc.return_value = ("TX_E2E_001", 10.0)

        # Mock SMTP dispatch — capture the Message-ID it returns
        dispatch_result = MagicMock(
            message_id="<e2e-msg-001@infra.sml.com>",
            recipient="dev@target.com",
            domain=target_domain,
            subject="test",
            accepted=True,
        )

        with patch.object(self.a, "enrich_domain", return_value=enriched), \
             patch.object(self.a, "send_pitch", return_value=dispatch_result):
            summary = self.a.run_cycle(
                verticals=(vertical,),
                output_root=self.root,
                xrpl_client=xrpl_client,
                balance_check_fn=self._healthy_balance_fn(),
            )

        # Outbound assertions
        self.assertEqual(summary["total_attempted"], 1)
        self.assertEqual(summary["total_sent"], 1)
        xrpl_client.send_demo_payment_for_usdc.assert_called_once()

        # State machine should show PITCH_DELIVERED with tx hash recorded
        sm = self.s.OutreachStateMachine()
        entry = sm.get_domain(target_domain)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["state"], self.s.STATE_PITCH_DELIVERED)
        self.assertEqual(entry["tx_hash"], "TX_E2E_001")

        # Thread map should now know which Message-ID belongs to which domain
        thread_map = self.rm.load_thread_map(self.root)
        self.assertEqual(thread_map.get("<e2e-msg-001@infra.sml.com>"), target_domain)

        # opctl status should show our domain in PITCH_DELIVERED
        code, out = self._opctl_run("status")
        self.assertEqual(code, 0)
        status = json.loads(out)
        self.assertGreaterEqual(
            status["domain_counts_by_state"].get(self.s.STATE_PITCH_DELIVERED, 0),
            1,
        )

        # ── PART 2: INBOUND REPLY (OPTOUT) ──
        # Simulate an inbound STOP reply to the pitch we just sent
        inbound = _make_inbound_reply_email(
            in_reply_to="<e2e-msg-001@infra.sml.com>",
            body="STOP please don't email me again",
        )
        mock_imap = _build_mock_imap([("1", inbound)])

        result = self.rm.poll_inbox(
            output_root=self.root,
            imap_factory=lambda h, p: mock_imap,
        )

        # Inbound assertions
        self.assertEqual(result.classified_optout, 1)
        self.assertEqual(result.fetched, 1)

        # Domain should NOW be OPTED_OUT (state machine mutated by the monitor)
        sm2 = self.s.OutreachStateMachine()
        entry_after = sm2.get_domain(target_domain)
        self.assertEqual(entry_after["state"], self.s.STATE_OPTED_OUT)

        # Operator queue should have one entry (the STOP reply)
        queue = self.rm.load_operator_queue(self.root)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["classification"], "OPTOUT")

        # opctl recent should show the OPTED_OUT transition as latest event
        code, out = self._opctl_run("recent", "5")
        self.assertEqual(code, 0)
        recent = json.loads(out)
        self.assertGreaterEqual(len(recent), 1)
        # The OPT_OUT update is the most recent state change for this domain
        latest_for_target = next(
            (e for e in recent if e["domain"] == target_domain), None
        )
        self.assertIsNotNone(latest_for_target)
        self.assertEqual(latest_for_target["state"], self.s.STATE_OPTED_OUT)

        # ── PART 3: SECOND CYCLE — DOMAIN MUST BE BLOCKED ──
        # Stage the same domain again; the cycle should refuse to pitch it
        self._stage_bounty(vertical, target_domain)

        with patch.object(self.a, "enrich_domain", return_value=enriched), \
             patch.object(self.a, "send_pitch", return_value=dispatch_result):
            summary2 = self.a.run_cycle(
                verticals=(vertical,),
                output_root=self.root,
                xrpl_client=xrpl_client,
                balance_check_fn=self._healthy_balance_fn(),
            )

        # Second cycle: attempted=1 (we tried) but sent=0 (blocked by opt-out)
        self.assertEqual(summary2["total_attempted"], 1)
        self.assertEqual(summary2["total_sent"], 0)


if __name__ == "__main__":
    unittest.main()
