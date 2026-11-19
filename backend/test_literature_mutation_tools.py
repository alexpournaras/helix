import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.literature import mutation_tools


class LiteratureMutationToolTests(unittest.TestCase):
    def test_delete_all_and_running_fetch_guard(self):
        fake_db = Mock()
        fake_db.delete_all.return_value = 12
        with patch.object(mutation_tools, "db", fake_db), patch.object(mutation_tools.runner, "any_running", return_value=False):
            result = mutation_tools.execute("delete_all_literature", {})
        self.assertEqual(result["deleted_count"], 12)
        self.assertEqual(result["audit"]["action"], "delete_all_literature")

        with patch.object(mutation_tools.runner, "any_running", return_value=True):
            self.assertIn("error", mutation_tools.execute("delete_all_literature", {}))

    def test_fetch_start_and_status(self):
        run_data = {"id": 7, "source": "pubmed", "term": "VHL", "max_results": 3, "sort": "relevance",
                    "tags": [], "status": "running"}
        run = SimpleNamespace(id=7, model_dump=lambda **_: run_data)
        with patch.object(mutation_tools.runner, "start", return_value=run) as start:
            result = mutation_tools.execute("fetch_literature", '{"source":"pubmed","term":"VHL","max_results":3}')
        self.assertEqual(result["scope"], "literature_fetch_started")
        self.assertEqual(result["run"]["id"], 7)
        self.assertEqual(start.call_args.args[0], "pubmed")
        self.assertEqual(start.call_args.args[1].term, "VHL")

        fake_db = Mock()
        fake_db.get_run.return_value = run
        with patch.object(mutation_tools, "db", fake_db):
            status = mutation_tools.execute("get_literature_fetch_run", {"id": 7})
        self.assertEqual(status["scope"], "literature_fetch_status")
        self.assertIn("error", mutation_tools.execute("fetch_literature", {"source": "bad", "term": "VHL"}))

    def test_cancel_one_or_all_fetches(self):
        with patch.object(mutation_tools.runner, "cancel", return_value=True):
            result = mutation_tools.execute("cancel_literature_fetch", {"id": 4})
        self.assertEqual(result["cancelled_ids"], [4])
        with patch.object(mutation_tools.runner, "cancel", return_value=False):
            self.assertIn("error", mutation_tools.execute("cancel_literature_fetch", {"id": 99}))
        with patch.object(mutation_tools.runner, "cancel_all", return_value=[4, 7]):
            result = mutation_tools.execute("cancel_all_literature_fetches", {})
        self.assertEqual(result["cancelled_count"], 2)
        self.assertEqual(result["cancelled_ids"], [4, 7])


if __name__ == "__main__":
    unittest.main()
