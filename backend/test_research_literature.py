"""1A.8 literature additions: flags, search_records, fetch_many, hit counts, enrich(citations=False),
find_existing, tag_origin (PLAN B8.8, B8.13, C9–C14). MockTransport only; synthetic records."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

import research_testkit  # noqa: F401  (installs the no-network guard)
from app.literature import enrich as enrich_mod
from app.literature.db import LiteratureDB
from app.literature.models import Article, Details, Section
from app.literature.sources import europepmc, ncbi, pubmed


def _pubmed_xml(pmid: str, *, pub_types=("Journal Article",), corrections=(), pmc: str | None = None) -> str:
    pts = "".join(f"<PublicationType>{p}</PublicationType>" for p in pub_types)
    ccs = "".join(f'<CommentsCorrections RefType="{r}"><RefSource>X</RefSource></CommentsCorrections>' for r in corrections)
    cc_list = f"<CommentsCorrectionsList>{ccs}</CommentsCorrectionsList>" if ccs else ""
    pmc_id = f'<ArticleId IdType="pmc">{pmc}</ArticleId>' if pmc else ""
    return (
        f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article><Journal><Title>J</Title>"
        f"<JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue></Journal>"
        f"<ArticleTitle>Synthetic title {pmid}</ArticleTitle><Abstract><AbstractText>Synthetic.</AbstractText></Abstract>"
        f"<PublicationTypeList>{pts}</PublicationTypeList></Article>{cc_list}</MedlineCitation>"
        f'<PubmedData><ArticleIdList><ArticleId IdType="pubmed">{pmid}</ArticleId>{pmc_id}</ArticleIdList></PubmedData>'
        f"</PubmedArticle>"
    )


def _set(*articles: str) -> bytes:
    return ("<PubmedArticleSet>" + "".join(articles) + "</PubmedArticleSet>").encode()


def _epmc_core(i: int, **extra) -> dict:
    return {"id": str(i), "source": "MED", "pmid": str(i), "title": f"Synthetic {i}", "abstractText": "Synthetic.",
            "pubYear": "2023", **extra}


class _NoThrottle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = [patch.object(ncbi._throttle, "wait", self._no_wait), patch.object(ncbi, "_key", lambda: "")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    @staticmethod
    async def _no_wait(interval):
        return None


class FlagTests(unittest.TestCase):
    def _pm(self, xml: str) -> Article:
        import xml.etree.ElementTree as ET
        return pubmed.parse(ET.fromstring(xml))

    def test_pubmed_retraction_from_publication_type_or_retraction_in(self):
        self.assertTrue(self._pm(_pubmed_xml("1", pub_types=("Retracted Publication",))).retracted)
        self.assertTrue(self._pm(_pubmed_xml("2", corrections=("RetractionIn",))).retracted)
        plain = self._pm(_pubmed_xml("3"))
        self.assertFalse(plain.retracted)
        self.assertFalse(plain.concern)

    def test_pubmed_concern_only_from_concern_in(self):
        self.assertTrue(self._pm(_pubmed_xml("4", corrections=("ExpressionOfConcernIn",))).concern)
        notice = self._pm(_pubmed_xml("5", pub_types=("Expression of Concern",), corrections=("ExpressionOfConcernFor",)))
        self.assertFalse(notice.concern)  # the notice itself, not the affected paper (spike X4/X5)
        self.assertFalse(self._pm(_pubmed_xml("6", corrections=("RetractionOf",))).retracted)

    def test_europepmc_flags(self):
        def corr(*types):
            return {"commentCorrectionList": {"commentCorrection": [{"type": t} for t in types]}}
        self.assertTrue(europepmc.parse(_epmc_core(1, pubTypeList={"pubType": ["Retracted Publication"]}), "MED:1").retracted)
        self.assertTrue(europepmc.parse(_epmc_core(2, **corr("Retraction in")), "MED:2").retracted)
        self.assertTrue(europepmc.parse(_epmc_core(3, **corr("Expression of concern in")), "MED:3").concern)
        notice = europepmc.parse(_epmc_core(4, **corr("Expression of concern for")), "MED:4")
        self.assertFalse(notice.concern)
        self.assertFalse(notice.retracted)

    def test_full_text_likely(self):
        self.assertTrue(Article(source="pubmed", source_id="1", pmcid="PMC1").full_text_likely)
        self.assertTrue(Article(source="europepmc", source_id="MED:1", pmcid="PMC1", open_access=True).full_text_likely)
        self.assertFalse(Article(source="europepmc", source_id="MED:1", pmcid="PMC1", open_access=False).full_text_likely)
        self.assertFalse(Article(source="europepmc", source_id="MED:1", open_access=True).full_text_likely)


class PubMedBatchTests(_NoThrottle):
    async def test_fetch_many_posts_in_batches_of_200_and_keeps_input_order(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertTrue(request.url.path.endswith("/efetch.fcgi"))
            form = parse_qs(request.content.decode())
            batch = form["id"][0].split(",")
            calls.append(batch)
            self.assertEqual(form["db"], ["pubmed"])
            # Return in reverse order and drop one ID, as a real server may.
            return httpx.Response(200, content=_set(*(_pubmed_xml(i) for i in reversed(batch) if i != "7")))

        wanted = [str(i) for i in range(1, 251)] + ["5"]  # one duplicate
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            articles = await pubmed.fetch_many(client, wanted)
        self.assertEqual([len(c) for c in calls], [200, 50])
        self.assertEqual([a.pmid for a in articles], [str(i) for i in range(1, 251) if i != 7])

    async def test_fetch_many_empty_makes_no_call(self):
        def handler(request):
            raise AssertionError("no request expected")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(await pubmed.fetch_many(client, []), [])

    async def test_fetch_many_skips_invalid_ids(self):
        seen = []

        def handler(request):
            seen.append(parse_qs(request.content.decode())["id"][0])
            return httpx.Response(200, content=_set(_pubmed_xml("12")))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            articles = await pubmed.fetch_many(client, ["12", "abc", ""])
        self.assertEqual(seen, ["12"])
        self.assertEqual(len(articles), 1)

    async def test_pubmed_count_uses_retmax_zero(self):
        def handler(request):
            self.assertEqual(request.url.params["retmax"], "0")
            self.assertEqual(request.url.params["term"], "x[tiab]")
            return httpx.Response(200, json={"esearchresult": {"count": "42", "idlist": []}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(await pubmed.count(client, "x[tiab]"), 42)

    async def test_pubmed_count_raises_on_query_error(self):
        def handler(request):
            return httpx.Response(200, json={"esearchresult": {"ERROR": "bad query"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ValueError):
                await pubmed.count(client, "x[zz]")


class EuropePmcSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_records_returns_parsed_core_articles_across_pages(self):
        pages = {"*": ([_epmc_core(1), _epmc_core(2)], "c2"), "c2": ([_epmc_core(3, pmcid="PMC3", isOpenAccess="Y")], "c3"),
                 "c3": ([], "c3")}
        seen = []

        def handler(request):
            params = request.url.params
            self.assertEqual(params["resultType"], "core")
            self.assertEqual(params["query"], 'TITLE_ABS:"x"')
            seen.append(params["cursorMark"])
            hits, nxt = pages[params["cursorMark"]]
            return httpx.Response(200, json={"hitCount": 3, "nextCursorMark": nxt, "resultList": {"result": hits}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            total, articles = await europepmc.search_records(client, 'TITLE_ABS:"x"', 10)
        self.assertEqual(total, 3)
        self.assertEqual([a.source_id for a in articles], ["MED:1", "MED:2", "MED:3"])
        self.assertEqual(articles[0].abstract, "Synthetic.")
        self.assertTrue(articles[2].full_text_likely)
        self.assertEqual(seen[:2], ["*", "c2"])

    async def test_search_records_respects_max_results(self):
        def handler(request):
            self.assertEqual(request.url.params["pageSize"], "2")
            return httpx.Response(200, json={"hitCount": 50, "nextCursorMark": "n",
                                             "resultList": {"result": [_epmc_core(1), _epmc_core(2)]}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            total, articles = await europepmc.search_records(client, "q", 2)
        self.assertEqual((total, len(articles)), (50, 2))

    async def test_count_uses_idlist_with_page_size_one(self):
        def handler(request):
            self.assertEqual(request.url.params["resultType"], "idlist")
            self.assertEqual(request.url.params["pageSize"], "1")
            return httpx.Response(200, json={"hitCount": 1234, "resultList": {"result": []}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(await europepmc.count(client, "q"), 1234)

    def test_scoped(self):
        self.assertEqual(europepmc.scoped('A OR B'), "(A OR B) AND (SRC:MED OR SRC:PMC)")


class EnrichCitationsTests(_NoThrottle):
    async def test_citations_false_skips_the_citations_call_but_keeps_references(self):
        paths = []

        def handler(request):
            paths.append(request.url.path)
            if request.url.path.endswith("/references"):
                return httpx.Response(200, json={"hitCount": 1, "referenceList": {"reference": [{"title": "Ref A", "pmid": "9"}]}})
            return httpx.Response(200, json={})

        article = Article(source="pubmed", source_id="1", pmid="1")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            _, details = await enrich_mod.enrich(client, article, Details(), citations=False)
            self.assertFalse(any(p.endswith("/citations") for p in paths), paths)
            self.assertEqual([r.title for r in details.references], ["Ref A"])
            paths.clear()
            await enrich_mod.enrich(client, article, Details())
            self.assertTrue(any(p.endswith("/citations") for p in paths), paths)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "literature.db"
        self.db = LiteratureDB(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_find_existing_by_pmid_pmcid_or_doi(self):
        a, _ = self.db.save_article(Article(source="pubmed", source_id="11", pmid="11", doi="10.1/abc"))
        b, _ = self.db.save_article(Article(source="pmc", source_id="PMC22", pmcid="PMC22"))
        self.assertEqual(self.db.find_existing(pmid="11").id, a)
        self.assertEqual(self.db.find_existing(pmcid="22").id, b)
        self.assertEqual(self.db.find_existing(pmcid="pmc22").id, b)
        self.assertEqual(self.db.find_existing(doi="https://doi.org/10.1/ABC").id, a)
        self.assertEqual(self.db.find_existing(pmid="99", doi="10.1/abc").id, a)
        self.assertIsNone(self.db.find_existing(pmid="99"))
        self.assertIsNone(self.db.find_existing())

    def test_find_existing_prefers_pmid_then_a_copy_with_full_text(self):
        self.db.save_article(Article(source="pubmed", source_id="5", pmid="5"))
        full, _ = self.db.save_article(Article(source="europepmc", source_id="MED:5", pmid="5"),
                                       details=Details(full_text=[Section(title="R", text="Text.")]))
        other, _ = self.db.save_article(Article(source="pmc", source_id="PMC8", pmcid="PMC8"))
        self.assertEqual(self.db.find_existing(pmid="5").id, full)
        self.assertEqual(self.db.find_existing(pmid="5", pmcid="PMC8").id, full)

    def test_tag_origin(self):
        user, _ = self.db.save_article(Article(source="pubmed", source_id="1", pmid="1"), tags=["mine"])
        ai, _ = self.db.save_article(Article(source="pubmed", source_id="2", pmid="2"), tags=["inv:3"], tag_origin="ai")
        self.assertEqual([(t.name, t.origin) for t in self.db.get(user).tags], [("mine", "user")])
        self.assertEqual([(t.name, t.origin) for t in self.db.get(ai).tags], [("inv:3", "ai")])
        with self.assertRaises(ValueError):
            self.db.save_article(Article(source="pubmed", source_id="3"), tags=["x"], tag_origin="robot")

    def test_flags_round_trip(self):
        i, _ = self.db.save_article(Article(source="pubmed", source_id="1", pmid="1", retracted=True, concern=True))
        item = self.db.get(i)
        self.assertIs(item.retracted, True)
        self.assertIs(item.concern, True)

    def test_migration_adds_flag_columns_to_an_old_database(self):
        old = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(old)
        conn.execute("CREATE TABLE literature (id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL, "
                     "url TEXT NOT NULL DEFAULT '', pmid TEXT, pmcid TEXT, doi TEXT, title TEXT NOT NULL DEFAULT '', "
                     "abstract TEXT NOT NULL DEFAULT '', authors TEXT NOT NULL DEFAULT '[]', journal TEXT NOT NULL DEFAULT '', "
                     "year INTEGER, pub_date TEXT NOT NULL DEFAULT '', volume TEXT NOT NULL DEFAULT '', "
                     "issue TEXT NOT NULL DEFAULT '', pages TEXT NOT NULL DEFAULT '', keywords TEXT NOT NULL DEFAULT '[]', "
                     "mesh TEXT NOT NULL DEFAULT '[]', publication_types TEXT NOT NULL DEFAULT '[]', "
                     "language TEXT NOT NULL DEFAULT '', open_access INTEGER, created_at TEXT NOT NULL, "
                     "updated_at TEXT NOT NULL, UNIQUE (source, source_id))")
        conn.execute("INSERT INTO literature (source, source_id, pmid, created_at, updated_at) "
                     "VALUES ('pubmed', '1', '1', 'x', 'x')")
        conn.commit()
        conn.close()
        db = LiteratureDB(old)
        item = db.find_existing(pmid="1")
        self.assertFalse(item.retracted)
        self.assertFalse(item.concern)


if __name__ == "__main__":
    unittest.main()
