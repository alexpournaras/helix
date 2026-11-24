"""1A.7 segmentation: sentence splitters, user segments, paper snapshots (PLAN B2.1, B8.1, B8.14).

Synthetic text only.
"""

import unittest

import research_testkit  # noqa: F401  (installs the no-network guard)
from app.literature.models import Details, Figure, Section
from app.research import ids
from app.research.segment import (
    SEGMENTER_VERSION,
    answer_segment,
    followup_segment,
    is_greek,
    note_segments,
    paper_snapshot,
    quote,
    split_paper,
    split_user,
    user_segments,
)


class UserSplitterTests(unittest.TestCase):
    def test_splits_on_terminators_followed_by_uppercase_digit_quote_or_bracket(self):
        text = 'It started last week. 3 days ago it got worse! "Odd," I thought? (Then) it stopped… Again.'
        self.assertEqual(split_user(text), [
            "It started last week.", "3 days ago it got worse!", '"Odd," I thought?', "(Then) it stopped…", "Again.",
        ])

    def test_no_split_before_lowercase_or_inside_decimals(self):
        self.assertEqual(split_user("I took 2.5 units. then nothing. Fine."), ["I took 2.5 units. then nothing.", "Fine."])

    def test_line_breaks_always_split(self):
        self.assertEqual(split_user("first line\nsecond line\r\n\nthird"), ["first line", "second line", "third"])

    def test_unicode_uppercase_starts_a_sentence(self):
        self.assertEqual(split_user("Ça va. Élan returns."), ["Ça va.", "Élan returns."])

    def test_greek_question_marks(self):
        text = "Τι είναι αυτό; Δεν ξέρω. Πότε θα περάσει; Σύντομα."
        self.assertEqual(split_user(text), ["Τι είναι αυτό;", "Δεν ξέρω.", "Πότε θα περάσει;", "Σύντομα."])

    def test_semicolon_is_not_a_terminator_in_english(self):
        self.assertEqual(split_user("One part; Two part."), ["One part; Two part."])

    def test_greek_flag_overrides_detection(self):
        self.assertEqual(split_user("A; B c.", greek=True), ["A; B c."])  # "A;" is < 3 chars → merged back
        self.assertEqual(split_user("Aa; Bb c.", greek=True), ["Aa;", "Bb c."])

    def test_short_pieces_merge_into_previous(self):
        self.assertEqual(split_user("Something happened. A. Then more."), ["Something happened. A.", "Then more."])

    def test_short_first_piece_merges_into_next(self):
        self.assertEqual(split_user("Ok\nthen it went on"), ["Ok then it went on"])

    def test_whitespace_is_normalised_and_empty_input(self):
        self.assertEqual(split_user("  a   lot \t of   space.  "), ["a lot of space."])
        self.assertEqual(split_user(""), [])
        self.assertEqual(split_user(" \n \n"), [])

    def test_abbreviations_do_not_split_user_text(self):
        self.assertEqual(split_user("Dr. Lee said so. Fine."), ["Dr. Lee said so.", "Fine."])

    def test_is_greek(self):
        self.assertTrue(is_greek("Καλημέρα, test"))
        self.assertFalse(is_greek("Hello α world"))


class UserSegmentTests(unittest.TestCase):
    def test_problem_sentences_get_u_ids(self):
        segs = user_segments("First thing. Second thing.\nThird thing.")
        self.assertEqual([s.seg_id for s in segs], ["U1", "U2", "U3"])
        self.assertEqual([s.origin for s in segs], ["problem"] * 3)
        self.assertTrue(all(ids.USER_SENTENCE.match(s.seg_id) for s in segs))

    def test_answer_segment(self):
        seg = answer_segment(7, "How long?", "About two weeks.")
        self.assertEqual(seg.seg_id, "A7")
        self.assertEqual(seg.text, "Q: How long? A: About two weeks.")
        self.assertEqual((seg.origin, seg.ref_id), ("answer", "7"))

    def test_answer_segment_is_capped_at_2000_chars(self):
        seg = answer_segment(1, "Q?", "x" * 5000)
        self.assertLessEqual(len(seg.text), 2000)
        self.assertTrue(seg.text.startswith("Q: Q? A: x"))
        self.assertTrue(seg.text.endswith("…"))

    def test_followup_segment(self):
        seg = followup_segment(3, "  What about  this?  ")
        self.assertEqual((seg.seg_id, seg.text, seg.origin, seg.ref_id), ("FU3", "What about this?", "followup", "3"))

    def test_note_segments(self):
        segs = note_segments(12, "Line one. Line two.")
        self.assertEqual([s.seg_id for s in segs], ["N12.s1", "N12.s2"])
        self.assertTrue(all(ids.NOTE_SENTENCE.match(s.seg_id) for s in segs))
        self.assertEqual({(s.origin, s.ref_id) for s in segs}, {("note", "12")})


class PaperSplitterTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(split_paper("We measured X. It rose. Then it fell."),
                         ["We measured X.", "It rose.", "Then it fell."])

    def test_never_splits_inside_brackets(self):
        text = "Values rose (see Fig. 2. Also Table 1). Next sentence here."
        self.assertEqual(split_paper(text), ["Values rose (see Fig. 2. Also Table 1).", "Next sentence here."])

    def test_unbalanced_bracket_does_not_block_later_splits(self):
        text = "Items were a) first. Then b) second. Done here."
        self.assertEqual(split_paper(text), ["Items were a) first.", "Then b) second.", "Done here."])
        text = "An open ( bracket here. Next one. Last one."
        self.assertEqual(split_paper(text), ["An open ( bracket here.", "Next one.", "Last one."])

    def test_never_splits_after_abbreviations(self):
        cases = [
            "Groups differed, e.g. Group A and B. Next.",
            "As reported by Smith et al. The cohort was large. Next.",
            "Shown in Fig. 3 and Figs. 4 and 5. Next.",
            "Compared with placebo vs. Treatment B was faster. Next.",
            "The dose was approx. 5 units. Next.",
            "See Table 2, i.e. The main table. Next.",
            "Patient No. 4 was excluded. Next.",
        ]
        for text in cases:
            with self.subTest(text=text):
                pieces = split_paper(text)
                self.assertEqual(len(pieces), 2, pieces)
                self.assertEqual(pieces[1], "Next.")

    def test_never_splits_after_initials(self):
        self.assertEqual(split_paper("J. A. Smith ran the trial. Next one."), ["J. A. Smith ran the trial.", "Next one."])
        self.assertEqual(split_paper("Data from Mary K. Jones were used. Next one."),
                         ["Data from Mary K. Jones were used.", "Next one."])

    def test_single_letter_after_lowercase_word_still_ends_a_sentence(self):
        self.assertEqual(split_paper("Levels of factor B. Patients improved."),
                         ["Levels of factor B.", "Patients improved."])

    def test_never_splits_inside_decimals(self):
        self.assertEqual(split_paper("The ratio was 1.52 (95% CI 1.10-2.01). Next one."),
                         ["The ratio was 1.52 (95% CI 1.10-2.01).", "Next one."])

    def test_closing_quote_or_bracket_stays_with_the_sentence(self):
        self.assertEqual(split_paper('They wrote "it is done." Next one.'), ['They wrote "it is done."', "Next one."])

    def test_line_breaks_split_papers_too(self):
        self.assertEqual(split_paper("Background: one\nMethods: two"), ["Background: one", "Methods: two"])


def _details() -> Details:
    return Details(
        full_text=[
            Section(title="", text="Lead paragraph one. Second sentence."),
            Section(title="Methods", text="We did A. We did B.\n\nAnother paragraph."),
            Section(title="Acknowledgements", text="We thank the staff.", kind="back"),
            Section(title="Acknowledgments", text="Funding was provided by Grant X.", kind="back"),
            Section(title="Author contributions", text="All authors wrote it.", kind="back"),
            Section(title="Notes", text="Some note.", kind="back"),
            Section(title="Conflicts of interest", text="None declared.", kind="back"),
            Section(title="Results", text="It worked.", kind="body"),
        ],
        figures=[
            Figure(kind="figure", label="Figure 1", caption="A flow chart."),
            Figure(kind="table", label="Table 1", caption="Baseline values.",
                   rows=[["Group", "n"], ["A", "10"], ["B", ""]], head_rows=1, footer="n, number."),
            Figure(kind="figure", label="Figure 2", caption=""),
            Figure(kind="table", label="Table 2", caption="", rows=[["x", "y"]], head_rows=0),
        ],
    )


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.snap = paper_snapshot("Aim: one thing. More.\nConclusion: done.", _details())
        self.by_id = {s.seg_id: s for s in self.snap.segments}

    def test_abstract_segments(self):
        self.assertEqual(self.by_id["AB.P1.s1"].text, "Aim: one thing.")
        self.assertEqual(self.by_id["AB.P1.s2"].text, "More.")
        self.assertEqual(self.by_id["AB.P2.s1"].text, "Conclusion: done.")
        self.assertEqual(self.by_id["AB.P1.s1"].section, "Abstract")

    def test_body_sentence_ids_follow_sections_and_paragraphs(self):
        self.assertEqual(self.by_id["S1.P1.s2"].text, "Second sentence.")
        self.assertEqual(self.by_id["S2.P1.s2"].text, "We did B.")
        self.assertEqual(self.by_id["S2.P2.s1"].text, "Another paragraph.")
        self.assertEqual(self.by_id["S2.P1.s1"].section, "Methods")

    def test_section_numbers_follow_source_order_including_skipped(self):
        self.assertEqual(self.by_id["S8.P1.s1"].text, "It worked.")

    def test_skip_rules(self):
        kept_sections = {s.section for s in self.snap.segments}
        self.assertNotIn("Acknowledgements", kept_sections)
        self.assertNotIn("Author contributions", kept_sections)
        self.assertNotIn("Notes", kept_sections)
        self.assertIn("Acknowledgments", kept_sections)  # mentions funding → kept
        self.assertIn("Conflicts of interest", kept_sections)
        self.assertEqual(self.snap.skipped_sections, ["Acknowledgements", "Author contributions", "Notes"])

    def test_notes_mentioning_conflicts_are_kept(self):
        snap = paper_snapshot("", Details(full_text=[
            Section(title="Body", text="Main text."),
            Section(title="Notes", text="The authors declare no competing interests.", kind="back"),
        ]))
        self.assertEqual(snap.skipped_sections, [])
        self.assertIn("S2.P1.s1", {s.seg_id for s in snap.segments})

    def test_tables_at_cell_level(self):
        self.assertEqual(self.by_id["T1.cap"].text, "Baseline values.")
        self.assertEqual(self.by_id["T1.r1.c1"].text, "Group")
        self.assertTrue(self.by_id["T1.r1.c1"].header)
        self.assertFalse(self.by_id["T1.r2.c2"].header)
        self.assertEqual(self.by_id["T1.r2.c2"].text, "10")
        self.assertNotIn("T1.r3.c2", self.by_id)  # empty cell: no segment
        self.assertEqual(self.by_id["T1.r3.c1"].text, "B")
        self.assertEqual(self.by_id["T1.foot"].text, "n, number.")
        self.assertEqual(self.by_id["T1.cap"].label, "Table 1")
        self.assertNotIn("T2.cap", self.by_id)
        self.assertEqual(self.by_id["T2.r1.c2"].text, "y")

    def test_figure_captions(self):
        self.assertEqual(self.by_id["F1.cap"].text, "A flow chart.")
        self.assertNotIn("F2.cap", self.by_id)

    def test_every_id_is_valid_and_unique(self):
        seg_ids = [s.seg_id for s in self.snap.segments]
        self.assertEqual(len(seg_ids), len(set(seg_ids)))
        self.assertTrue(all(ids.is_paper_segment(i) for i in seg_ids), seg_ids)

    def test_document_order(self):
        seg_ids = [s.seg_id for s in self.snap.segments]
        self.assertLess(seg_ids.index("AB.P2.s1"), seg_ids.index("S1.P1.s1"))
        self.assertLess(seg_ids.index("S8.P1.s1"), seg_ids.index("F1.cap"))
        self.assertLess(seg_ids.index("F1.cap"), seg_ids.index("T1.cap"))
        self.assertEqual([s.order for s in self.snap.segments], list(range(len(seg_ids))))

    def test_hash_is_stable_and_content_sensitive(self):
        again = paper_snapshot("Aim: one thing. More.\nConclusion: done.", _details())
        self.assertEqual(self.snap.hash, again.hash)
        self.assertEqual(len(self.snap.hash), 64)
        changed = paper_snapshot("Aim: one thing. More.\nConclusion: other.", _details())
        self.assertNotEqual(self.snap.hash, changed.hash)
        self.assertEqual(self.snap.version, SEGMENTER_VERSION)

    def test_coverage_label(self):
        self.assertEqual(self.snap.coverage, "full")
        self.assertEqual(paper_snapshot("Only an abstract.", Details()).coverage, "abstract_only")
        self.assertTrue(self.snap.has_body)

    def test_round_trip(self):
        from app.research.segment import Snapshot
        self.assertEqual(Snapshot.from_dict(self.snap.to_dict()), self.snap)

    def test_quote_in_document_order_with_ellipsis(self):
        self.assertEqual(quote(self.snap, ["S2.P1.s2", "S1.P1.s1"]), "Lead paragraph one. … We did B.")
        self.assertEqual(quote(self.snap, ["S2.P1.s1", "S2.P1.s2"]), "We did A. We did B.")

    def test_quote_of_a_row_covers_its_cells(self):
        self.assertEqual(quote(self.snap, ["T1.r2"]), "A | 10")

    def test_quote_unknown_id_raises(self):
        with self.assertRaises(KeyError):
            quote(self.snap, ["S99.P1.s1"])


if __name__ == "__main__":
    unittest.main()
