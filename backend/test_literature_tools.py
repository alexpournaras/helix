import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.literature import tools
from app.literature.db import LiteratureDB
from app.literature.models import Article, Details, Figure, Ref, Section


class LiteratureToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = LiteratureDB(Path(self.temp.name) / "literature.db")
        self.article_id, _ = self.db.save_article(
            Article(source="pubmed", source_id="123", pmid="123", title="TP53 in cancer", abstract="TP53 suppresses tumors.",
                    authors=["Ada Researcher"], journal="Helix Journal", year=2025),
            details=Details(full_text=[Section(title="Results", text="The experiment found a strong effect.")], full_text_from="pmc",
                            figures=[Figure(label="Figure 1", caption="A result", image="https://example.test/figure.jpg")],
                            references=[Ref(title="Earlier work")]),
        )
        self.db.add_tag(self.article_id, "hi-ai-read-this")
        self.db_patch = patch.object(tools, "db", self.db)
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_search_read_and_recent_are_read_only(self):
        listed = tools.execute("list_literature", {})
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["papers"][0]["id"], self.article_id)
        self.assertNotIn("abstract_excerpt", listed["papers"][0])
        found = tools.execute("search_literature", {"query": "TP53"})
        self.assertEqual(found["papers"][0]["id"], self.article_id)
        tagged = tools.execute("search_literature", {"tag": "HI-AI-READ-THIS"})
        self.assertEqual(tagged["papers"][0]["id"], self.article_id)
        self.assertIn("hi-ai-read-this", tagged["papers"][0]["tags"])
        tags = tools.execute("list_literature_tags", {})
        self.assertEqual(tags["tags"][0]["name"], "hi-ai-read-this")
        read = tools.execute("get_literature", {"id": self.article_id})
        self.assertEqual(read["scope"], "abstract")
        self.assertNotIn("full_text_sections", read["paper"])
        full = tools.execute("get_literature_full_text", {"id": self.article_id})
        self.assertEqual(full["paper"]["full_text_sections"][0]["title"], "Results")
        self.assertTrue(full["audit"]["complete"])
        figures = tools.execute("get_literature_figures", {"id": self.article_id})
        self.assertEqual(figures["paper"]["figures_and_tables"][0]["label"], "Figure 1")
        references = tools.execute("get_literature_references", {"id": self.article_id})
        self.assertEqual(references["paper"]["references"][0]["title"], "Earlier work")
        self.assertEqual(read["sources"][0]["title"], "TP53 in cancer")
        recent = tools.execute("list_recent_literature", {"limit": 1})
        self.assertEqual(recent["papers"][0]["id"], self.article_id)

    def test_invalid_tool_arguments_are_safe(self):
        self.assertIn("error", tools.execute("get_literature", {"id": "bad"}))
        self.assertIn("error", tools.execute("unknown", {}))


if __name__ == "__main__":
    unittest.main()
