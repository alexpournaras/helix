// Display details for the three literature integrations, shared by their pages and the library.
export const LIT_SOURCES = {
  pubmed: {
    id: "pubmed",
    name: "PubMed",
    short: "PubMed",
    color: "cyan",
    docs: "https://pubmed.ncbi.nlm.nih.gov/help/#search-tags",
    intro: "Abstracts and citations for biomedical literature from NCBI. Each article comes with its MeSH terms, keywords and links to PMC and the DOI.",
    syntax: <>PubMed search syntax works, for example <code>VHL[tiab]</code> or <code>"von Hippel-Lindau Disease"[mh]</code>. To add one article, paste its link, PMCID, <code>PMID: …</code> or DOI.</>,
    ncbi: true,
  },
  pmc: {
    id: "pmc",
    name: "PubMed Central",
    short: "PMC",
    color: "magenta",
    docs: "https://pmc.ncbi.nlm.nih.gov/search/",
    intro: "Free full-text articles archived by NCBI. A plain term matches anywhere in the full text, so results are broader than PubMed. Helix saves the title, abstract and citation details.",
    syntax: <>Limit to titles and abstracts with <code>VHL[tiab]</code>, or to a field such as <code>VHL[body]</code>. To add one article, paste its link, PMCID, <code>PMID: …</code> or DOI.</>,
    ncbi: true,
  },
  europepmc: {
    id: "europepmc",
    name: "Europe PMC",
    short: "Europe PMC",
    color: "yellow",
    docs: "https://europepmc.org/searchsyntax",
    intro: "EMBL-EBI’s index of PubMed, PMC, preprints, patents and more. Free and needs no key.",
    syntax: <>Europe PMC syntax works, for example <code>TITLE:VHL</code>, <code>ABSTRACT:"von Hippel-Lindau"</code> or <code>SRC:PPR</code> for preprints. To add one article, paste its link, PMCID, <code>PMID: …</code> or DOI.</>,
    ncbi: false,
  },
};

export const SOURCE_IDS = Object.keys(LIT_SOURCES);

export const RUN_STATUS = {
  running: ["badge-cyan", "Running"],
  done: ["badge-ok", "Done"],
  failed: ["badge-bad", "Failed"],
  cancelled: ["", "Cancelled"],
  interrupted: ["badge-bad", "Interrupted"],
};

export const formatNumber = (n) => (n == null ? "–" : n.toLocaleString());

export const formatWhen = (iso) => {
  if (!iso) return "–";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "–";
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay
    ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
};

export const formatAuthors = (authors, max = 3) => {
  if (!authors?.length) return "";
  return authors.length > max ? `${authors.slice(0, max).join(", ")} et al.` : authors.join(", ");
};
