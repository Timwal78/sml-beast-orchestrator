"""BB7 — autonomous backlink-outreach agent.

See docs/BB7_DESIGN.md (SEALED) for the full architecture. Module landing
order is locked: guardrails -> state -> enricher/templates/verifier ->
escrow -> agent. Each downstream module routes authorization through
guardrails.OutreachGuardrails before moving money or sending mail.
"""
