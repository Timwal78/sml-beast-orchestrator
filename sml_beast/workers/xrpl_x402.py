"""
XRPL / x402 agentic infrastructure vertical worker.

Reads XRPL_X402 brief + XRPL_X402_SILOS keyword space, pulls live SERP per
keyword through the x402 proxy, and writes a reviewed MDX + JSON-LD bundle
to output/xrpl/<slug>/. No auto-deploy.
"""

from sml_beast.content.briefs    import XRPL_X402
from sml_beast.content.keywords  import XRPL_X402_SILOS
from sml_beast.content.generator import write_page
from sml_beast.workers.base      import Worker


class XrplX402Worker(Worker):
    name = "xrpl_x402"
    BRIEF = XRPL_X402
    SILOS = XRPL_X402_SILOS

    def process_keyword(self, silo_name: str, keyword: str, serp_data: dict) -> str:
        return write_page(self.output_dir, self.brief, silo_name, keyword, serp_data)
