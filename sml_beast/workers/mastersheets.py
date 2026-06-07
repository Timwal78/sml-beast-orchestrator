"""
MasterSheets vertical worker.

Reads the MASTERSHEETS canonical brief + MASTERSHEETS_SILOS keyword space.
The base Worker handles the SERP fetch and gap analysis; this subclass
just turns a gap-overlaid page brief into a disk artifact.
"""

from sml_beast.content.briefs import MASTERSHEETS
from sml_beast.content.generator import write_page
from sml_beast.content.keywords import MASTERSHEETS_SILOS
from sml_beast.workers.base import Worker


class MasterSheetsWorker(Worker):
    name = "mastersheets"
    vertical = "mastersheets"
    BRIEF = MASTERSHEETS
    SILOS = MASTERSHEETS_SILOS

    def process_keyword(self, silo_name: str, keyword: str, page_brief: dict) -> str:
        return write_page(self.output_dir, page_brief, silo_name, keyword)
