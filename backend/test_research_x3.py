"""Spike X3 scoring (PLAN 1A.11): valid IDs per extractor and reconciliation against hand-paired claims.
Pure functions over saved results; synthetic data, no network."""

import json
import unittest

from app.literature.models import Details, Section
from app.research import claims as C
from app.research.schemas import Claim
from app.research.segment import paper_snapshot
from app.research.steps import read as R
from eval.spikes import x3_dual_extraction as X

SNAP = paper_snapshot("Synthetic abstract.", Details(full_text=[Section(title="Results", text=(
    "Noise exposure raised the odds of tinnitus. Ear wax was not associated with tinnitus."))], full_text_from="pmc"))
IDS = [s.seg_id for s in SNAP.segments if s.section == "Results"]


def result(claims, rejected=(), clusters=()):
    return {"claims": list(claims), "rejected": list(rejected), "clusters": [list(c) for c in clusters]}


def claim(label, n, cited, valid):
    return {"extractor": label, "n": n, "claim": f"claim {label}{n}", "cited_ids": cited, "valid_ids": valid}


class IdStatsTests(unittest.TestCase):
    def test_valid_ids_are_counted_per_extractor_including_rejected_claims(self):
        r = result([claim("m0", 1, 2, 2), claim("m0", 2, 3, 2), claim("m1", 1, 1, 1)],
                   rejected=[{"extractor": "m1", "cited_ids": 2, "reason": "no valid sentence IDs"}])
        stats = X.id_stats({"P1": r})
        self.assertEqual(stats["m0"], {"claims": 2, "rejected": 0, "cited_ids": 5, "valid_ids": 4, "rate": 0.8})
        self.assertEqual(stats["m1"], {"claims": 2, "rejected": 1, "cited_ids": 3, "valid_ids": 1, "rate": 1 / 3})

    def test_no_ids_cited_gives_no_rate(self):
        self.assertIsNone(X.id_stats({"P1": result([claim("m0", 1, 0, 0)])})["m0"]["rate"])


class PairMatchTests(unittest.TestCase):
    def test_a_hand_pair_counts_when_reconciliation_put_both_claims_in_one_cluster(self):
        r = result([claim("m0", 1, 1, 1), claim("m0", 2, 1, 1), claim("m1", 1, 1, 1), claim("m1", 2, 1, 1)],
                   clusters=[["m0:1", "m1:2"], ["m0:2"], ["m1:1"]])
        got = X.pair_match({"P1": r}, {"P1": [["m0:1", "m1:2"], ["m0:2", "m1:1"]]})
        self.assertEqual((got["gold_pairs"], got["matched"], got["rate"]), (2, 1, 0.5))
        self.assertEqual(got["missed"], [("P1", "m0:2", "m1:1")])
        self.assertEqual(got["extra"], [])  # every merge reconciliation made was a hand pair

    def test_merges_without_a_hand_pair_are_listed(self):
        r = result([claim("m0", 1, 1, 1), claim("m1", 1, 1, 1)], clusters=[["m0:1", "m1:1"]])
        got = X.pair_match({"P1": r}, {"P1": []})
        self.assertIsNone(got["rate"])
        self.assertEqual(got["extra"], [("P1", "m0:1", "m1:1")])

    def test_unknown_claim_refs_in_the_gold_list_are_an_error(self):
        r = result([claim("m0", 1, 1, 1)], clusters=[["m0:1"]])
        with self.assertRaises(ValueError):
            X.pair_match({"P1": r}, {"P1": [["m0:1", "m1:9"]]})


class SummariseTests(unittest.TestCase):
    def test_claims_are_numbered_per_extractor_and_clusters_use_their_refs(self):
        def checked(text, ids):
            raw = Claim.model_validate({"claim": text, "sentence_ids": ids, "evidence_type": "finding",
                                        "population": "adults", "design": "cohort", "n": None, "effect": None,
                                        "limitations": "", "links": []})
            ok, _ = C.check_claim(raw, allowed=IDS, snapshot=SNAP, profile_n=None, hypothesis_keys=set())
            return ok
        a = checked("Noise raised tinnitus odds", [IDS[0], "S9.P9.s9"])
        b = checked("Noise exposure raised tinnitus", [IDS[0]])
        w = checked("Ear wax not associated", [IDS[1]])
        ext = R.Extraction(checked=[("m0", None, 1, "pack", a), ("m1", None, 1, "pack", b), ("m1", None, 1, "pack", w)],
                           rejected=[(json.dumps({"claim": "x", "sentence_ids": ["S8.P1.s1", "S8.P1.s1"]}), "no valid sentence IDs", "m0")],
                           clusters=[C.Cluster(("m1", b), [("m0", a)]), C.Cluster(("m1", w))], borderline=1)
        out = X.summarise(ext)
        self.assertEqual([c["ref"] for c in out["claims"]], ["m0:1", "m1:1", "m1:2"])
        self.assertEqual((out["claims"][0]["cited_ids"], out["claims"][0]["valid_ids"]), (2, 1))  # unknown ID dropped
        self.assertEqual(out["clusters"], [["m1:1", "m0:1"], ["m1:2"]])
        self.assertEqual(out["rejected"], [{"extractor": "m0", "reason": "no valid sentence IDs", "cited_ids": 1, "claim": "x"}])
        self.assertAlmostEqual(X.id_stats({"P": out})["m0"]["rate"], 1 / 3)  # 1 valid of 2 + 1 (rejected) cited


if __name__ == "__main__":
    unittest.main()
