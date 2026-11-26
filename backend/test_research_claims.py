"""1E.1 claim checks and reconciliation (PLAN B8.14). Synthetic text only."""

import unittest

from app.literature.models import Details, Section
from app.research import claims as C
from app.research.schemas import Claim
from app.research.segment import paper_snapshot

BODY = ("Participants were enrolled in the HELIX cohort (NCT01234567). "
        "The odds ratio for tinnitus was 1.50 (95% CI 1.10 to 2.05; p = 0.01) in 1,234 adults. "
        "Noise exposure was common.")


def snap():
    return paper_snapshot("Synthetic abstract about tinnitus.",
                          Details(full_text=[Section(title="Results", text=BODY)], full_text_from="pmc"))


def claim(ids, **kw):
    base = {"claim": "Noise exposure raised the odds of tinnitus", "sentence_ids": ids, "evidence_type": "finding",
            "design": "cohort", "links": [{"hypothesis_key": "noise", "direction": "supports", "strength": "moderate",
                                           "case_match": "high"}]}
    base.update(kw)
    return Claim.model_validate(base)


class ClaimCheckTests(unittest.TestCase):
    def setUp(self):
        self.s = snap()
        self.ids = [x.seg_id for x in self.s.segments if x.section == "Results"]

    def test_registry_ids(self):
        self.assertEqual([r["id"] for r in C.registry_ids(self.s)], ["NCT01234567"])

    def test_numbers(self):
        text = "OR 1.50 (95% CI 1,10 to 2.05), p<.001, n = 1,234, difference −0.4"
        for x in (1.5, 1.50, 1.1, 2.05, 1234, -0.4, 0.001):
            self.assertTrue(C.number_in(x, text), x)
        for x in (1.05, 2.5, 123):
            self.assertFalse(C.number_in(x, text), x)
        self.assertTrue(C.p_in("<0.001", text))

    def test_id_outside_the_pack_is_rejected(self):
        got, why = C.check_claim(claim(["S1.P1.s9"]), allowed=self.ids, snapshot=self.s, profile_n=None,
                                 hypothesis_keys={"noise"})
        self.assertIsNone(got)
        self.assertEqual(why, "no valid sentence IDs")
        got, _ = C.check_claim(claim([self.ids[1]]), allowed=[self.ids[0]], snapshot=self.s, profile_n=None,
                               hypothesis_keys={"noise"})
        self.assertIsNone(got)  # in the paper but not in the pack shown

    def test_quote_order_effect_and_n(self):
        effect = {"measure": "OR", "value": 1.5, "ci_low": 1.1, "ci_high": 2.05, "p": "0.01"}
        got, _ = C.check_claim(claim([self.ids[2], self.ids[1]], effect=effect, n=1234), allowed=self.ids,
                               snapshot=self.s, profile_n=None, hypothesis_keys={"noise"})
        self.assertEqual(got.sentence_ids, [self.ids[2], self.ids[1]])
        self.assertTrue(got.quote.startswith("The odds ratio"))  # document order
        self.assertEqual(got.claim.effect.value, 1.5)
        self.assertEqual(got.claim.n, 1234)
        got, _ = C.check_claim(claim([self.ids[1]], effect={**effect, "value": 1.7}, n=999), allowed=self.ids,
                               snapshot=self.s, profile_n="999 adults screened", hypothesis_keys={"noise"})
        self.assertIsNone(got.claim.effect)  # 1.7 isn't in the cited sentence
        self.assertTrue(got.checks["effect_cleared"])
        self.assertEqual(got.claim.n, 999)  # in the profile
        got, _ = C.check_claim(claim([self.ids[1]], n=50), allowed=self.ids, snapshot=self.s, profile_n=None,
                               hypothesis_keys={"noise"})
        self.assertIsNone(got.claim.n)

    def test_unknown_hypothesis_keys_are_dropped(self):
        got, _ = C.check_claim(claim([self.ids[1]]), allowed=None, snapshot=self.s, profile_n=None,
                               hypothesis_keys={"other"})
        self.assertEqual(got.claim.links, [])
        self.assertTrue(got.checks["neutral"])


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.s = snap()
        self.ids = [x.seg_id for x in self.s.segments if x.section == "Results"]

    def ck(self, ids, text):
        return C.check_claim(claim(ids, claim=text), allowed=None, snapshot=self.s, profile_n=None,
                             hypothesis_keys={"noise"})[0]

    def test_matching_keeps_the_claim_citing_fewer_sentences(self):
        a = self.ck([self.ids[1], self.ids[2]], "Noise exposure raised the odds of tinnitus in adults")
        b = self.ck([self.ids[1]], "Noise exposure raised the odds of tinnitus")
        c = self.ck([self.ids[0]], "The cohort was registered")
        clusters, unjudged = C.reconcile([("A", a), ("B", b), ("A", c)])
        self.assertEqual(unjudged, [])
        self.assertEqual(sorted(cl.agreement for cl in clusters), [1, 2])
        pair = next(cl for cl in clusters if cl.members)
        self.assertIs(pair.kept[1], b)

    def test_borderline_pairs_wait_for_the_judge(self):
        a = self.ck([self.ids[1]], "Noise exposure raised the odds of tinnitus")
        b = self.ck([self.ids[1]], "Adults with loud work had more ringing")  # same sentence, other words
        clusters, unjudged = C.reconcile([("A", a), ("B", b)])
        self.assertEqual(unjudged, [])  # Jaccard too low even for borderline: two claims
        self.assertEqual(len(clusters), 2)
        b2 = self.ck([self.ids[1]], "Noise exposure was linked with more tinnitus in adults")
        _, unjudged = C.reconcile([("A", a), ("B", b2)])
        self.assertEqual(unjudged, [(0, 1)])
        clusters, _ = C.reconcile([("A", a), ("B", b2)], same={(0, 1): True})
        self.assertEqual([cl.agreement for cl in clusters], [2])

    def test_one_extractors_claims_never_merge(self):
        a = self.ck([self.ids[1]], "Noise exposure raised the odds of tinnitus")
        clusters, _ = C.reconcile([("A", a), ("A", a)])
        self.assertEqual(len(clusters), 2)
