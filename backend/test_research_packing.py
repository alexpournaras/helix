"""1A.7 packing: section-aware packs in document order, no caps unless a profile sets one (PLAN B8.14).

Synthetic text only.
"""

import unittest

import research_testkit  # noqa: F401  (installs the no-network guard)
from app.literature.models import Details, Figure, Section
from app.research import ids, tokens
from app.research.packing import pack_snapshot, render_segments
from app.research.segment import paper_snapshot


def _sentence(i: int, words: int = 12) -> str:
    return "Sentence " + " ".join(f"word{i}x{j}" for j in range(words)) + "."


def _paragraph(start: int, n: int) -> str:
    return " ".join(_sentence(start + k) for k in range(n))


def _paper(sections: list[tuple[str, int, int]], tables: int = 0) -> Details:
    """sections: (title, paragraphs, sentences per paragraph)."""
    full_text, counter = [], 0
    for title, paras, per in sections:
        text = []
        for _ in range(paras):
            text.append(_paragraph(counter, per))
            counter += per
        full_text.append(Section(title=title, text="\n\n".join(text)))
    figures = [
        Figure(kind="table", label=f"Table {t + 1}", caption=f"Caption {t}.",
               rows=[["Head A", "Head B"], *[[f"r{r}a", f"r{r}b"] for r in range(30)]], head_rows=1,
               footer="Footer.")
        for t in range(tables)
    ]
    return Details(full_text=full_text, figures=figures)


class RenderTests(unittest.TestCase):
    def test_render_format(self):
        snap = paper_snapshot("One. Two.", Details(
            full_text=[Section(title="Methods", text="We did A. We did B.\n\nNew para.")],
            figures=[Figure(kind="table", label="Table 1", caption="Cap.", rows=[["G", "n"], ["A", ""], ["", "5"]],
                            head_rows=1, footer="Foot.")],
        ))
        text = render_segments(snap.segments)
        self.assertEqual(text, "\n".join([
            "### Abstract",
            "[AB.P1.s1] One.",
            "[AB.P1.s2] Two.",
            "",
            "### S1 Methods",
            "[S1.P1.s1] We did A.",
            "[S1.P1.s2] We did B.",
            "",
            "[S1.P2.s1] New para.",
            "",
            "### T1 Table 1",
            "[T1.cap] Cap.",
            "[T1.r1] (header) [T1.r1.c1] G | [T1.r1.c2] n",
            "[T1.r2] [T1.r2.c1] A | —",
            "[T1.r3] — | [T1.r3.c2] 5",
            "[T1.foot] Foot.",
        ]))


class PackingTests(unittest.TestCase):
    def test_small_paper_is_one_pack_with_every_segment(self):
        snap = paper_snapshot("Abstract here.", _paper([("Intro", 2, 3), ("Methods", 2, 3)], tables=1))
        result = pack_snapshot(snap, max_tokens=32000)
        self.assertEqual(len(result.packs), 1)
        self.assertEqual(result.packs[0].seg_ids, [s.seg_id for s in snap.segments])
        self.assertEqual(result.coverage, "full")
        self.assertEqual(result.dropped_ids, [])

    def test_packs_follow_document_order_and_cover_everything_once(self):
        snap = paper_snapshot("Abstract here.", _paper([(f"Sec{i}", 4, 6) for i in range(8)], tables=2))
        result = pack_snapshot(snap, max_tokens=1500)
        self.assertGreater(len(result.packs), 3)
        flat = [i for p in result.packs for i in p.seg_ids]
        body = [s.seg_id for s in snap.segments]
        # Every segment appears, in document order; only table header rows may repeat.
        self.assertEqual([i for i in dict.fromkeys(flat)], body)
        repeated = [i for i in flat if flat.count(i) > 1]
        self.assertTrue(all(snap.get(i).header for i in repeated), repeated)
        self.assertEqual([p.no for p in result.packs], list(range(1, len(result.packs) + 1)))

    def test_packs_respect_the_token_limit(self):
        snap = paper_snapshot("Abstract here.", _paper([(f"Sec{i}", 4, 6) for i in range(8)], tables=2))
        result = pack_snapshot(snap, max_tokens=1500)
        for p in result.packs:
            self.assertFalse(p.oversize)
            self.assertLessEqual(p.tokens, 1500)
            self.assertEqual(p.tokens, tokens.estimate(p.text))

    def test_sections_are_not_split_when_they_fit_in_a_fresh_pack(self):
        # Each section is ~ 40% of a pack: two fit together, a third starts a new pack.
        snap = paper_snapshot("", _paper([(f"Sec{i}", 1, 5) for i in range(5)]))
        one = tokens.estimate(render_segments([s for s in snap.segments if s.seg_id.startswith("S1.")]))
        result = pack_snapshot(snap, max_tokens=int(one * 2.5))
        for p in result.packs:
            parts = {i.split(".")[0] for i in p.seg_ids}
            for part in parts:
                section_ids = [s.seg_id for s in snap.segments if s.seg_id.split(".")[0] == part]
                self.assertTrue(set(section_ids) <= set(p.seg_ids), f"{part} split across packs")

    def test_large_section_splits_at_paragraphs_and_repeats_its_heading(self):
        snap = paper_snapshot("", _paper([("Results", 6, 6)]))
        para = tokens.estimate(render_segments([s for s in snap.segments if s.seg_id.startswith("S1.P1.")]))
        result = pack_snapshot(snap, max_tokens=int(para * 2.5))
        self.assertGreater(len(result.packs), 1)
        for p in result.packs:
            self.assertTrue(p.text.startswith("### S1 Results"))
            paras = {i.rsplit(".", 1)[0] for i in p.seg_ids}
            for para_id in paras:
                para_ids = [s.seg_id for s in snap.segments if s.seg_id.rsplit(".", 1)[0] == para_id]
                self.assertTrue(set(para_ids) <= set(p.seg_ids), f"{para_id} split across packs")

    def test_huge_paragraph_splits_at_sentences(self):
        snap = paper_snapshot("", _paper([("Results", 1, 40)]))
        result = pack_snapshot(snap, max_tokens=400)
        self.assertGreater(len(result.packs), 2)
        self.assertEqual([i for p in result.packs for i in p.seg_ids], [s.seg_id for s in snap.segments])

    def test_oversize_sentence_gets_its_own_flagged_pack(self):
        long = "Long " + " ".join(f"w{j}" for j in range(400)) + "."
        snap = paper_snapshot("", Details(full_text=[Section(title="X", text=f"Short one. {long} Short two.")]))
        result = pack_snapshot(snap, max_tokens=300)
        flagged = [p for p in result.packs if p.oversize]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].seg_ids, ["S1.P1.s2"])
        self.assertEqual([i for p in result.packs for i in p.seg_ids], ["S1.P1.s1", "S1.P1.s2", "S1.P1.s3"])

    def test_large_table_splits_by_rows_and_repeats_header_rows(self):
        snap = paper_snapshot("", _paper([], tables=1))
        result = pack_snapshot(snap, max_tokens=250)
        self.assertGreater(len(result.packs), 1)
        for p in result.packs:
            self.assertTrue(p.text.startswith("### T1 Table 1"))
            self.assertIn("T1.r1.c1", p.seg_ids)
        self.assertIn("T1.cap", result.packs[0].seg_ids)
        self.assertIn("T1.foot", result.packs[-1].seg_ids)
        rows = [i.rsplit(".", 1)[0] for p in result.packs for i in p.seg_ids
                if ids.TABLE_CELL.match(i) and not i.startswith("T1.r1.")]
        self.assertEqual(rows, sorted(rows, key=lambda r: int(r.split(".r")[1])))

    def test_pack_cap_drops_later_packs_and_marks_partial(self):
        snap = paper_snapshot("Abstract here.", _paper([(f"Sec{i}", 4, 6) for i in range(8)]))
        full = pack_snapshot(snap, max_tokens=1500)
        capped = pack_snapshot(snap, max_tokens=1500, max_packs=2)
        self.assertEqual(len(capped.packs), 2)
        self.assertEqual(capped.coverage, "partial")
        kept = {i for p in capped.packs for i in p.seg_ids}
        self.assertEqual(capped.dropped_ids, [s.seg_id for s in snap.segments if s.seg_id not in kept])
        self.assertEqual(full.coverage, "full")
        self.assertEqual(pack_snapshot(snap, max_tokens=1500, max_packs=None).packs, full.packs)

    def test_priority_cap_keeps_results_over_earlier_sections(self):
        """Budget (PLAN B17.5 item 4): a late results section survives a two-pack cap; packs stay in document order."""
        sections = [("Introduction", 4, 6), ("Background", 4, 6), ("Methods", 4, 6), ("Statistical analysis", 4, 6),
                    ("Results", 4, 6), ("Discussion", 4, 6)]
        snap = paper_snapshot("Abstract here.", _paper(sections, tables=1))
        plain = pack_snapshot(snap, max_tokens=1500, max_packs=2)
        prio = pack_snapshot(snap, max_tokens=1500, max_packs=2, priority=True)
        kept = lambda r: {s.section for s in snap.segments if s.seg_id in {i for p in r.packs for i in p.seg_ids}}  # noqa: E731
        self.assertNotIn("Results", kept(plain))  # today's cap loses the results
        self.assertIn("Results", kept(prio))
        self.assertIn("Abstract", kept(prio))
        self.assertLessEqual(len(prio.packs), 2)
        self.assertEqual(prio.coverage, "partial")
        self.assertIn("Introduction", prio.dropped_sections)
        self.assertNotIn("Results", prio.dropped_sections)
        order = [s.seg_id for s in snap.segments]
        shown = [i for p in prio.packs for i in p.seg_ids]
        self.assertEqual(shown, sorted(shown, key=order.index))  # document order
        # under the cap, priority changes nothing
        self.assertEqual(pack_snapshot(snap, max_tokens=1500, priority=True).packs, pack_snapshot(snap, 1500).packs)

    def test_abstract_only(self):
        snap = paper_snapshot("Only. An abstract.", Details())
        result = pack_snapshot(snap)
        self.assertEqual(result.coverage, "abstract_only")
        self.assertEqual(result.packs[0].seg_ids, ["AB.P1.s1", "AB.P1.s2"])

    def test_empty_snapshot(self):
        result = pack_snapshot(paper_snapshot("", Details()))
        self.assertEqual(result.packs, [])
        self.assertEqual(result.coverage, "abstract_only")

    def test_contains_accepts_row_ids(self):
        snap = paper_snapshot("", _paper([], tables=1))
        p = pack_snapshot(snap).packs[0]
        self.assertTrue(p.contains("T1.r3"))
        self.assertTrue(p.contains("T1.r3.c2"))
        self.assertFalse(p.contains("T1.r99"))
        self.assertFalse(p.contains("S1.P1.s1"))

    def test_default_limit_comes_from_settings(self):
        from app.research.settings import QUALITY
        from app.research.packing import DEFAULT_MAX_TOKENS
        self.assertEqual(DEFAULT_MAX_TOKENS, QUALITY["extraction"]["max_pack_tokens"])

    def test_greek_text_packs_with_script_aware_estimate(self):
        greek = " ".join("Αυτή είναι μια πρόταση αριθμός %d." % i for i in range(60))
        snap = paper_snapshot("", Details(full_text=[Section(title="Ενότητα", text=greek)]))
        result = pack_snapshot(snap, max_tokens=500)
        self.assertGreater(len(result.packs), 1)
        self.assertTrue(all(p.tokens <= 500 for p in result.packs))


if __name__ == "__main__":
    unittest.main()
