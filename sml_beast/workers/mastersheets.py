"""
MasterSheets vertical worker.

Reads MASTERSHEETS canonical brief + MASTERSHEETS_SILOS keyword space, pulls
live SERP per keyword through the x402 proxy, and writes a reviewed MDX +
JSON-LD bundle to output/mastersheets/<slug>/. No auto-deploy.
"""

from sml_beast.content.briefs    import MASTERSHEETS
from sml_beast.content.keywords  import MASTERSHEETS_SILOS
from sml_beast.content.generator import write_page
from sml_beast.workers.base      import Worker


class MasterSheetsWorker(Worker):
    name = "mastersheets"
    BRIEF = MASTERSHEETS
    SILOS = MASTERSHEETS_SILOS

    def process_keyword(self, silo_name: str, keyword: str, serp_data: dict) -> str:
        return write_page(self.output_dir, self.brief, silo_name, keyword, serp_data)
