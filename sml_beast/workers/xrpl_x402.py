"""
SML Institutional Rails (IRL) / x402 Paywall vertical worker.

Reads the XRPL_X402 canonical brief + XRPL_X402_SILOS keyword space. Same
loop as the MasterSheets worker — gap analysis happens in the base class;
this subclass just renders the gap-overlaid page brief.
"""

from sml_beast.content.briefs import XRPL_X402
from sml_beast.content.generator import write_page
from sml_beast.content.keywords import XRPL_X402_SILOS
from sml_beast.workers.base import Worker


class XrplX402Worker(Worker):
    name = "xrpl_x402"
    vertical = "xrpl_x402"
    BRIEF = XRPL_X402
    SILOS = XRPL_X402_SILOS

    def process_keyword(self, silo_name: str, keyword: str, page_brief: dict) -> str:
        return write_page(self.output_dir, page_brief, silo_name, keyword)
