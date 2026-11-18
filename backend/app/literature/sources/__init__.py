"""One module per literature integration. Each exposes the same three things:

    ID, NAME
    SORTS: {key: (label, the source's own sort value)}, DEFAULT_SORT
    async search(client, term, max_results, sort) -> (total_hits, [source_id, ...])
    async preview(client, term, max_results, sort) -> (total_hits, [Preview, ...])   # summaries, no full fetch
    async fetch(client, source_id) -> (Article, Details) | None   # one article; Details holds what
                                                                  # the source itself gave beyond it
Everything else (full text, references, citations…) is added by literature.enrich.
"""

from . import europepmc, pmc, pubmed

REGISTRY = {m.ID: m for m in (pubmed, pmc, europepmc)}
