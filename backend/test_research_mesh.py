"""1A.9 MeSH module: lookup, cache, entry-term fallback, tree → system mapping (PLAN B8.1, C36, spike X8).

Lookups replay recorded E-utilities responses (test_fixtures/mesh/, made by eval/record_mesh.py); MeSH
vocabulary only.
"""

import gzip
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx

from research_testkit import ResearchTestCase
from app.literature.sources import ncbi
from app.research import mesh, transport

FIXTURES = Path(__file__).resolve().parent / "test_fixtures" / "mesh"

EXPECTED = {  # term → (descriptor UI, name, systems) from the recordings
    "tinnitus": ("D014012", "Tinnitus", ["ent", "neurology"]),
    "headache": ("D006261", "Headache", ["neurology"]),
    "cephalalgia": ("D006261", "Headache", ["neurology"]),  # an entry term
    "chest pain": ("D002637", "Chest Pain", ["cardiovascular"]),
    "low back pain": ("D017116", "Low Back Pain", ["musculoskeletal"]),
    "cough": ("D003371", "Cough", ["respiratory"]),
    "dyspnea": ("D004417", "Dyspnea", ["respiratory"]),
    "abdominal pain": ("D015746", "Abdominal Pain", ["digestive"]),
    "urticaria": ("D014581", "Urticaria", ["skin", "immunology_allergy"]),
    "dysuria": ("D053159", "Dysuria", ["urogenital"]),
    "fatigue": ("D005221", "Fatigue", ["general"]),
    "fever": ("D005334", "Fever", ["general"]),
    "depression": ("D003863", "Depression", ["mental_health"]),
    "vertigo": ("D014717", "Vertigo", ["ent", "neurology"]),
    "hearing loss": ("D034381", "Hearing Loss", ["ent", "neurology"]),
    "pruritus": ("D011537", "Pruritus", ["skin"]),
    "D014012": ("D014012", "Tinnitus", ["ent", "neurology"]),
}
UNVERIFIED = ["palpitations", "blurred vision", "zzqx flarbitis"]


async def _no_wait(interval):
    return None


class _MeshCase(ResearchTestCase):
    def setUp(self):
        super().setUp()
        self._throttle = patch.object(ncbi._throttle, "wait", _no_wait)
        self._throttle.start()

    def tearDown(self):
        self._throttle.stop()
        super().tearDown()


class LookupTests(_MeshCase):
    async def test_recorded_terms(self):
        with transport.use_transport(transport.ReplayTransport(FIXTURES)):
            for term, (ui, name, systems) in EXPECTED.items():
                with self.subTest(term=term):
                    d = await mesh.lookup(term, self.db)
                    self.assertIsNotNone(d)
                    self.assertEqual((d.ui, d.name, d.systems), (ui, name, systems))
                    self.assertTrue(d.tree_numbers)
                    self.assertIn(name, d.entry_terms)

    async def test_unknown_terms_are_unverified(self):
        with transport.use_transport(transport.ReplayTransport(FIXTURES)):
            for term in UNVERIFIED:
                with self.subTest(term=term):
                    self.assertIsNone(await mesh.lookup(term, self.db))

    async def test_free_search_needs_an_entry_term_match(self):
        """X8: "palpitations" free-searches to Panic Disorder, which doesn't list the term → unverified."""
        def search(query):
            return httpx.Response(200, json={"esearchresult": {"idlist": [] if "[MeSH Terms]" in query else ["1"]}})

        def handler(request):
            if request.url.path.endswith("esearch.fcgi"):
                return search(request.url.params["term"])
            return httpx.Response(200, json={"result": {"uids": ["1"], "1": {
                "ds_meshui": "D010146", "ds_meshterms": ["Panic Disorder"], "ds_idxlinks": [{"treenum": "F03.080.725"}]}}})

        with transport.use_transport(httpx.MockTransport(handler)):
            self.assertIsNone(await mesh.lookup("palpitations", self.db))
            self.assertEqual((await mesh.lookup("panic disorder", self.db, refresh=True)).ui, "D010146")

    async def test_non_descriptor_records_are_ignored(self):
        def handler(request):
            if request.url.path.endswith("esearch.fcgi"):
                return httpx.Response(200, json={"esearchresult": {"idlist": ["2"]}})
            return httpx.Response(200, json={"result": {"uids": ["2"], "2": {"ds_meshui": "C000001", "ds_meshterms": ["x"]}}})
        with transport.use_transport(httpx.MockTransport(handler)):
            self.assertIsNone(await mesh.lookup("x", self.db))

    async def test_empty_term(self):
        self.assertIsNone(await mesh.lookup("   ", self.db))


class CacheTests(_MeshCase):
    async def test_second_lookup_uses_the_cache_case_insensitively(self):
        with transport.use_transport(transport.ReplayTransport(FIXTURES)):
            first = await mesh.lookup("tinnitus", self.db)
        with transport.use_transport(transport.BlockedTransport()):
            again = await mesh.lookup("  Tinnitus ", self.db)
        self.assertEqual(again, first)
        row = self.db.one("SELECT * FROM mesh_cache WHERE term = 'tinnitus'")
        self.assertEqual(row["descriptor_ui"], "D014012")
        self.assertIn("C09.218.458.670", mesh._list(row["tree_numbers"]))
        self.assertTrue(row["scope_note"])

    async def test_misses_are_cached_then_retried_after_the_ttl(self):
        with transport.use_transport(transport.ReplayTransport(FIXTURES)):
            self.assertIsNone(await mesh.lookup("palpitations", self.db))
        with transport.use_transport(transport.BlockedTransport()):
            self.assertIsNone(await mesh.lookup("palpitations", self.db))  # cached miss: no request
        with self.db.tx() as conn:
            conn.execute("UPDATE mesh_cache SET fetched_at = '2000-01-01T00:00:00.000Z' WHERE term = 'palpitations'")
        with transport.use_transport(transport.BlockedTransport()):
            with self.assertRaises(transport.NetworkBlocked):
                await mesh.lookup("palpitations", self.db)

    async def test_refresh_bypasses_the_cache(self):
        with transport.use_transport(transport.ReplayTransport(FIXTURES)):
            await mesh.lookup("cough", self.db)
        with transport.use_transport(transport.BlockedTransport()):
            with self.assertRaises(transport.NetworkBlocked):
                await mesh.lookup("cough", self.db, refresh=True)

    async def test_verify_dedupes_and_lists_dropped_terms(self):
        with transport.use_transport(transport.ReplayTransport(FIXTURES)):
            verified, dropped = await mesh.verify(["headache", "cephalalgia", "palpitations", "", "Headache"], self.db)
        self.assertEqual([d.ui for d in verified], ["D006261"])
        self.assertEqual(dropped, ["palpitations"])


class MappingTests(_MeshCase):
    def test_table_is_valid(self):
        table = mesh.load_mapping()
        mesh.validate_mapping(table)
        self.assertIn("general", table["systems"])

    def test_bad_tables_are_rejected(self):
        with self.assertRaises(ValueError):
            mesh.validate_mapping({"systems": {"a": "A"}, "prefixes": {"C01": "b"}})
        with self.assertRaises(ValueError):
            mesh.validate_mapping({"systems": {"a": "A"}, "prefixes": {"C1": "a"}})
        with self.assertRaises(ValueError):
            mesh.validate_mapping({"systems": {}, "prefixes": {}})

    def test_longest_prefix_wins(self):
        cases = {
            "C23.888.592.612.233": ["cardiovascular"],  # chest pain, not neurology (X8)
            "C23.888.592.612.441": ["neurology"],  # headache
            "C23.888.592.612.999": ["general"],  # other pain
            "C23.888.592.100": ["neurology"],
            "C10.228": ["neurology"],
            "C07.550.100": ["ent"],  # pharyngeal, not mouth
            "C07.100": ["mouth_dental"],
            "C12.050.351.500.100": ["female_reproductive"],
        }
        for tree, systems in cases.items():
            with self.subTest(tree=tree):
                self.assertEqual(mesh.systems_for([tree]), systems)

    def test_null_and_unknown_prefixes_give_no_system(self):
        self.assertEqual(mesh.systems_for(["C22.100"]), [])  # animal diseases → null
        self.assertEqual(mesh.systems_for(["F02.830"]), [])
        self.assertEqual(mesh.systems_for(["Z01.100"]), [])
        self.assertEqual(mesh.systems_for(["C100.1"]), [])  # "C10" must match whole segments only

    def test_union_over_trees_in_first_seen_order(self):
        self.assertEqual(mesh.systems_for(["C09.218", "C10.597", "C09.100"]), ["ent", "neurology"])

    def test_union_with_model_proposed_systems(self):
        d = mesh.Descriptor(ui="D1", name="x", tree_numbers=["C14.280"])
        self.assertEqual(mesh.union_systems([d], ["cardiovascular", "mental_health", "made_up"]),
                         ["cardiovascular", "mental_health"])


class RecordTransportTests(ResearchTestCase):
    async def test_gzip_responses_are_recorded_and_replayed(self):
        """Regression: RecordTransport passed on Content-Encoding with an already-decoded body."""
        payload = b'{"ok": true}'

        def handler(request):
            return httpx.Response(200, headers={"content-encoding": "gzip", "content-type": "application/json"},
                                  content=gzip.compress(payload))

        folder = Path(tempfile.mkdtemp(dir=self.tmp))
        async with httpx.AsyncClient(transport=transport.RecordTransport(folder, inner=httpx.MockTransport(handler))) as c:
            self.assertEqual((await c.get("https://example.test/a?x=1")).json(), {"ok": True})
        async with httpx.AsyncClient(transport=transport.ReplayTransport(folder)) as c:
            self.assertEqual((await c.get("https://example.test/a?x=1")).json(), {"ok": True})
