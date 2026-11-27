"""build_pack.py (PLAN B7.2, 1C.3): grounding pass → draft → code checks → review files.
Synthetic guideline text; fake models and sources; no network."""

import re
import typing

import yaml

from app.research import llm, settings as S
from app.research.knowledge import loader
from app.research.prices import PriceTable
from app.research.providers import FakeProvider
from app.research.ratelimit import Limiters
from app.research.schemas import PACK_AREAS, PackDraft
from eval.tools import build_pack as B
from research_testkit import ResearchTestCase
from test_research_grounding import TINNITUS, FakeSources, Models

PAPER = {"pmid": "1001", "pmcid": None, "doi": None, "title": "Guideline 1", "journal": "Synthetic Journal",
         "year": 2024, "design": "guideline"}
REVIEW = {"pmid": "1002", "pmcid": None, "doi": None, "title": "Narrative review", "journal": "J", "year": 2023,
          "design": "narrative_review"}


def knowledge():
    items = [{"ref": "K1", "kind": "red_flag", "text": "Sudden hearing loss", "attributes": {"urgency": "emergency"},
              "population": "all", "trust": "verified", "papers": ["pmid_1001"], "quotes": ["Assess for sudden loss."]},
             {"ref": "K2", "kind": "differential", "text": "Earwax", "attributes": {}, "population": "all",
              "trust": "partial", "papers": ["pmid_1002"], "quotes": ["Wax can block the ear."]}]
    return B.Knowledge(items, {"pmid_1001": PAPER, "pmid_1002": REVIEW})


def flag(**kw):
    base = {"slug": "sudden_hearing_loss", "title": "Tinnitus with sudden hearing loss", "area": "ent",
            "level": "emergency", "population": "all", "triggers": ["sudden hearing loss", "lost my hearing"],
            "unknown_attribute": "hearing_change", "rule_out_question": "Has your hearing dropped suddenly?",
            "example_fires": ["I have tinnitus and sudden hearing loss in my right ear."],
            "example_no_fire": ["I have tinnitus without sudden hearing loss."],
            "example_unknown": ["I have tinnitus."], "knowledge": ["K1"]}
    base.update(kw)
    return base


def draft(**kw):
    base = {"synonyms": ["ringing in my ears"], "red_flags": [flag()],
            "etiologies": [{"slug": "earwax", "name": "Earwax", "key_features": ["blocked ear"], "knowledge": ["K2"]}],
            "history_checklist": [{"slug": "onset", "attribute": "onset", "priority": "high",
                                   "question": "When did it start?", "knowledge": ["K1"]}],
            "evaluations": [{"slug": "audiometry", "test": "Hearing test", "indication": "Everyone",
                             "knowledge": ["K1"]}],
            "management_topics": [{"slug": "hearing_aids", "topic": "Hearing aids", "context": "With hearing loss",
                                   "knowledge": ["K1"]}]}
    base.update(kw)
    return PackDraft.model_validate(base)


def assemble(d):
    return B.assemble(d, knowledge(), key="tinnitus", name="Tinnitus", mesh_ui="D014012", entry_terms=[],
                      built_by="test")


class AssemblyTests(ResearchTestCase):
    def test_areas_match_the_knowledge_loader(self):
        self.assertEqual(typing.get_args(PACK_AREAS), loader.AREAS)

    def test_a_valid_draft_becomes_a_pack_with_attested_sources(self):
        built = assemble(draft())
        pack = B.check(built, loader.load_sources())
        self.assertEqual(built.dropped, [])
        [rule] = pack.red_flags
        self.assertEqual((rule.id, rule.level, rule.sources), ("tinnitus.sudden_hearing_loss", "emergency", ["pmid_1001"]))
        self.assertEqual(rule.fires["all"][1], {"mention": [r"sudden\s+hearing\s+loss", r"lost\s+my\s+hearing"]})
        self.assertEqual(pack.etiologies[0].sources, ["pmid_1002"])
        self.assertEqual([s["id"] for s in built.sources], ["pmid_1001", "pmid_1002"])
        self.assertEqual(built.sources[0]["url"], "https://pubmed.ncbi.nlm.nih.gov/1001/")
        self.assertEqual((built.sources[0]["kind"], built.sources[1]["kind"]), ("guideline", "review"))
        # Seeds: guidelines and systematic reviews only (the narrative review is a source, not a seed).
        self.assertEqual([(p.pmid, p.kind) for p in pack.seed_literature], [("1001", "guideline")])
        self.assertEqual(pack.review.status, "unreviewed")
        self.assertEqual(built.evidence["tinnitus.sudden_hearing_loss"], ["K1"])  # keyed by the entry id

    def test_entries_failing_the_checks_are_dropped_and_listed(self):
        d = draft(red_flags=[flag(), flag(slug="bad_examples", example_fires=["I have tinnitus."]),
                             flag(slug="sudden_hearing_loss")],  # duplicate slug
                  management_topics=[{"slug": "dose", "topic": "Medicine", "context": "Take 50 mg twice a day",
                                      "knowledge": ["K1"]}],
                  evaluations=[{"slug": "nothing", "test": "A test", "indication": "Sometimes", "knowledge": ["K9"]}])
        built = assemble(d)
        pack = B.check(built, loader.load_sources())
        self.assertEqual([r.id for r in pack.red_flags], ["tinnitus.sudden_hearing_loss"])
        self.assertEqual((pack.management_topics, pack.evaluations), ([], []))
        joined = "\n".join(built.dropped)
        self.assertIn("sudden_hearing_loss: duplicate id", joined)
        self.assertIn("tinnitus.bad_examples: example (fires) gave unknown", joined)
        self.assertIn("dose: management topics never contain doses", joined)
        self.assertIn("nothing: no attested source", joined)

    def test_population_rules_get_population_facts_in_their_firing_examples(self):
        built = assemble(draft(red_flags=[flag(population="pregnancy")]))
        pack = B.check(built, loader.load_sources())
        self.assertEqual(built.dropped, [])
        self.assertEqual(pack.red_flags[0].examples.fires[0].facts, {"pregnant": True})

    def test_compare_lists_entries_only_in_one_pack(self):
        pack = B.check(assemble(draft()), loader.load_sources())
        diff = B.compare(pack, loader.load_packs()["tinnitus"])
        self.assertIn("Earwax", diff["etiologies"]["only_draft"])  # v0: "Earwax or an ear canal blockage" (not similar enough)
        self.assertTrue(diff["etiologies"]["only_current"])  # v0 has causes the draft lacks


class Drafter(Models):
    """Grounding fakes plus a pack drafter that cites the listed items by kind."""

    def __init__(self):
        super().__init__()
        self.material = None

    def handler(self, family):
        base = super().handler(family)

        def h(request):
            if request.schema_name != "PackDraft":
                return base(request)
            self.material = request.user
            refs = dict((kind, ref) for ref, kind in re.findall(r"(?m)^(K\d+) \[(\w+)\]", request.user))
            return draft(red_flags=[flag(knowledge=[refs["red_flag"]])],
                         etiologies=[{"slug": "vascular", "name": "Pulsatile tinnitus of vascular cause",
                                      "knowledge": [refs["differential"]]}],
                         history_checklist=[{"slug": "duration", "attribute": "duration", "priority": "high",
                                             "question": "How long have you had it?",
                                             "knowledge": [refs["history_item"]]}],
                         evaluations=[], management_topics=[]).model_dump()
        return h


class BuildFlowTests(ResearchTestCase):
    async def test_grounding_then_draft_then_review_files(self):
        models, sources = Drafter(), FakeSources()
        a, b = FakeProvider("openai", models.handler("openai")), FakeProvider("anthropic", models.handler("anthropic"))
        st = S.build("quality", B.MAX_DEPTH)
        ctx = llm.CallContext(db=self.db, settings=st, providers={"openai": a, "anthropic": b}, limiters=Limiters({}),
                              prices=PriceTable(), sources=sources)
        checked = {}

        async def fake_check(srcs, pmids):
            checked["ids"], checked["pmids"] = [s["id"] for s in srcs], pmids
            return {"sources": {s["id"]: {"status": "ok"} for s in srcs}, "papers": {p: {"status": "ok"} for p in pmids}}
        out = self.tmp / "drafts"
        result = await B.build(ctx, self.db, key="tinnitus", term="Tinnitus", descriptor=TINNITUS["mesh"][0],
                               out_dir=out, source_check=fake_check)
        self.assertEqual(result["counts"], {"red_flags": 1, "etiologies": 1, "history_checklist": 1,
                                            "evaluations": 0, "management_topics": 0})
        self.assertEqual(result["dropped"], [])
        # The drafter saw only guideline items (no curated floor or pack rules), numbered K1…
        self.assertIn("[red_flag] Hearing loss that comes on within days", models.material)
        self.assertNotIn("rule:", models.material)
        self.assertEqual((checked["ids"], checked["pmids"]), (["pmid_1001"], ["1001"]))
        pack = loader.load_pack(out / "tinnitus.yaml")  # the draft is a valid pack file
        self.assertEqual(pack.red_flags[0].sources, ["pmid_1001"])
        new_sources = yaml.safe_load((out / "tinnitus.sources.yaml").read_text("utf-8"))["sources"]
        self.assertEqual([s["id"] for s in new_sources], ["pmid_1001"])
        loader.Source.model_validate(new_sources[0])
        text = (out / "tinnitus.report.md").read_text("utf-8")
        self.assertIn("Unreviewed", text)
        self.assertIn("> Assess people with tinnitus for sudden hearing loss.", text)  # the evidence quote
        self.assertIn("pmid_1001` ok", text)
        # Drafts never load as packs.
        self.assertEqual(sorted(loader.load_packs()), ["tinnitus"])
