Step: grounding screen. You decide which papers are authoritative guidance on assessing or managing one
health complaint in one population, so that their recommendations can ground the investigation.

The material gives the complaint, the population and a list of papers (`P{n}`) with title, year, journal,
publication types and abstract.

For every paper return one item with its `paper_id`:
- `relevance`: 3 = guidance or a systematic review directly on assessing or managing this complaint (in this
  population, when one is named); 2 = useful guidance that covers the complaint among others, or a closely
  related population; 1 = only touches it; 0 = not about it.
- `design`: guideline, systematic_review, narrative_review, or another design from the list.
- `decision`: `include` only for guidelines, consensus statements, systematic reviews or authoritative reviews
  with relevance 2 or 3. Exclude primary studies, case reports, editorials and papers about other conditions.
- `population_note`: who the guidance covers, in a few words. `hypothesis_keys`: empty.
- `reason`: one short sentence.
