"""Catalog of planned integrations. Nothing calls these services yet; this drives the admin UI."""

from typing import Literal

from pydantic import BaseModel

from .config import ProviderKeys

Kind = Literal["ai", "data"]
Status = Literal["planned", "key_found", "connected"]


class Integration(BaseModel):
    id: str
    name: str
    kind: Kind
    description: str
    docs_url: str
    env_key: str | None = None  # variable that enables it, if any
    status: Status = "planned"


CATALOG: list[Integration] = [
    Integration(id="ollama", name="Ollama", kind="ai", description="Open-weight models running on your own hardware.", docs_url="https://ollama.com"),
    Integration(id="openai", name="OpenAI", kind="ai", description="GPT models through the OpenAI API.", docs_url="https://platform.openai.com/docs", env_key="OPENAI_API_KEY"),
    Integration(id="anthropic", name="Claude", kind="ai", description="Anthropic's Claude models through the Claude API.", docs_url="https://docs.claude.com", env_key="ANTHROPIC_API_KEY"),
    Integration(id="gemini", name="Gemini", kind="ai", description="Google's Gemini models.", docs_url="https://ai.google.dev", env_key="GEMINI_API_KEY"),
    Integration(id="pubmed", name="PubMed", kind="data", description="Biomedical literature from NCBI. Works without a key; a key raises the rate limit.", docs_url="https://www.ncbi.nlm.nih.gov/books/NBK25501/", env_key="NCBI_API_KEY"),
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
        out.append(item.model_copy(update={"status": "key_found" if found else "planned"}))
    return out
