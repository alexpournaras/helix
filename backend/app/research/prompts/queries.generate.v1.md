Step: search queries. You write literature search queries for one research hypothesis about a person's health
problem, for the sources listed in the material.

The material gives the hypothesis (title, description, mechanism, discriminators), the complaint and its MeSH
descriptors with entry terms, the knowledge vocabulary (the differential item's wording and key features, and
pack MeSH terms and synonyms), population terms, the sources to use, the maximum number of queries, the query
types required this round, and the queries already run.

Syntax (only these fields; anything else returns nothing):
- Europe PMC: `TITLE_ABS:"…"`, `TITLE:`, `ABSTRACT:`, `KW:`, `MESH:"…"`, `PUB_TYPE:"review"` (also
  `"systematic review"`, `"meta-analysis"`, `"practice guideline"`, `"guideline"`, `"randomized controlled
  trial"`), `LANG:eng`, `OPEN_ACCESS:y`, `HAS_FT:y`, `PUB_YEAR:[2015 TO 2026]`, AND / OR / NOT, parentheses.
  Don't add `SRC:`; code limits the search to PubMed and PMC records.
- PubMed: `"…"[tiab]`, `[ti]`, `[ab]`, `"…"[mh]`, `[majr]`, `[pt]`, `[la]`, `[dp]`, `[sh]`, AND / OR / NOT,
  parentheses.

Return `queries`, each with `source`, `query`, `query_type` (`guideline`, `systematic_review`, `review`, `rct`,
`observational`, `diagnostic`, `mechanism`, `other`) and a one-line `rationale`:
- Combine the condition's terms (MeSH and synonyms, ORed) with the case's key features or the population when
  that narrows usefully. Search for evidence about the hypothesis in a person like this one, not the general topic.
- Include every required query type. Prefer reviews and guidelines; add primary studies for what reviews
  won't cover (e.g. a discriminating feature).
- Balance quotes and parentheses. Don't repeat a query already run, even reworded.
- No personal details from the case: medical terms only.
