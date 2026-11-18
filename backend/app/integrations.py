"""Catalog of integrations shown in the admin. `implemented` marks the ones Helix can already use."""

from typing import Literal

from pydantic import BaseModel

from .config import ProviderKeys
from .literature.sources import REGISTRY as LITERATURE_SOURCES

Kind = Literal["ai", "data"]
Status = Literal["planned", "ready", "key_found", "connected"]


class SortOption(BaseModel):
    id: str
    label: str


class Integration(BaseModel):
    id: str
    name: str
    kind: Kind
    description: str
    docs_url: str
    env_key: str | None = None  # variable that enables it, if any
    key_optional: bool = False  # works without env_key; the key only raises limits
    configurable: bool = False  # has its own page in the admin
    implemented: bool = False
    category: str | None = None  # "literature": listed on Literature → Fetch articles
    sort_options: list[SortOption] = []  # literature sources: the orders their search supports
    default_sort: str | None = None
    status: Status = "planned"


CATALOG: list[Integration] = [
    Integration(id="ollama", name="Ollama", kind="ai", description="Open-weight models on your own hardware, or Ollama cloud models.", docs_url="https://docs.ollama.com", configurable=True, implemented=True),
    Integration(id="openai", name="OpenAI", kind="ai", description="GPT models through the OpenAI API.", docs_url="https://developers.openai.com/api/docs", env_key="OPENAI_API_KEY", configurable=True, implemented=True),
    Integration(id="anthropic", name="Claude", kind="ai", description="Anthropic's Claude models through the Claude API.", docs_url="https://docs.claude.com", env_key="ANTHROPIC_API_KEY"),
    Integration(id="gemini", name="Gemini", kind="ai", description="Google's Gemini models.", docs_url="https://ai.google.dev", env_key="GEMINI_API_KEY"),
    Integration(id="pubmed", name="PubMed", kind="data", description="Biomedical abstracts and citations from NCBI. Free; a key raises the rate limit.", docs_url="https://www.ncbi.nlm.nih.gov/books/NBK25501/", env_key="NCBI_API_KEY", key_optional=True, configurable=True, implemented=True, category="literature"),
    Integration(id="pmc", name="PubMed Central", kind="data", description="Free full-text articles archived by NCBI. Shares PubMed's key and rate limit.", docs_url="https://pmc.ncbi.nlm.nih.gov/tools/developers/", env_key="NCBI_API_KEY", key_optional=True, configurable=True, implemented=True, category="literature"),
    Integration(id="europepmc", name="Europe PMC", kind="data", description="PubMed, PMC, preprints and more from EMBL-EBI. Free, no key.", docs_url="https://europepmc.org/RestfulWebService", configurable=True, implemented=True, category="literature"),
    Integration(id="clinvar", name="ClinVar", kind="data", description="How human variants relate to disease, with clinical significance.", docs_url="https://www.ncbi.nlm.nih.gov/clinvar/"),
    Integration(id="cbioportal", name="cBioPortal", kind="data", description="Cancer genomics studies: mutations, copy number and clinical data.", docs_url="https://www.cbioportal.org/api"),
    Integration(id="uniprot", name="UniProt", kind="data", description="Protein sequences and what they do.", docs_url="https://www.uniprot.org/help/api"),
    Integration(id="ensembl", name="Ensembl", kind="data", description="Genome annotation, genes and predicted variant effects.", docs_url="https://rest.ensembl.org"),
    Integration(id="clinicaltrials", name="ClinicalTrials.gov", kind="data", description="Registered clinical studies worldwide.", docs_url="https://clinicaltrials.gov/data-api/api"),
]


def catalog_with_status() -> list[Integration]:
    present = ProviderKeys().present()
    out = []
    for item in CATALOG:
        found = item.env_key is not None and item.env_key.lower() in present
        status = "key_found" if found else "ready" if item.implemented and (item.env_key is None or item.key_optional) else "planned"
        update = {"status": status}
        source = LITERATURE_SOURCES.get(item.id)
        if source:
            update["sort_options"] = [SortOption(id=k, label=label) for k, (label, _) in source.SORTS.items()]
            update["default_sort"] = source.DEFAULT_SORT
        out.append(item.model_copy(update=update))
    return out
