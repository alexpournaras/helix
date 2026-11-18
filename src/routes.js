import { Bot, BookOpen, Database, FolderKanban, LayoutDashboard, Plug, Settings } from "lucide-react";

// `blurb` explains a not-yet-built section in plain terms.
export const NAV = [
  {
    items: [{ id: "dashboard", path: "/", label: "Dashboard", icon: LayoutDashboard }],
  },
  {
    section: "Research",
    items: [
      { id: "literature", path: "/literature", label: "Literature", icon: BookOpen },
      {
        id: "projects", path: "/projects", label: "Projects", icon: FolderKanban, soon: true,
        blurb: "A project gathers the patients, datasets, papers and AI runs behind one research question.",
      },
      {
        id: "datasets", path: "/datasets", label: "Datasets", icon: Database, soon: true,
        blurb: "Import genomic, clinical and variant data from files or public portals like cBioPortal.",
      },
      {
        id: "ai-runs", path: "/ai-runs", label: "AI runs", icon: Bot, soon: true,
        blurb: "Ask Ollama, OpenAI, Claude and other models to analyse your data, then review what they found.",
      },
    ],
  },
  {
    section: "Workspace",
    items: [
      { id: "integrations", path: "/integrations", label: "Integrations", icon: Plug },
      { id: "settings", path: "/settings", label: "Settings", icon: Settings },
    ],
  },
];

// Pages that aren't in the sidebar. `parent` builds the breadcrumb and keeps the sidebar entry
// it descends from highlighted.
const HIDDEN = [
  { id: "ollama", path: "/integrations/ollama", label: "Ollama", parent: "integrations" },
  { id: "literature-fetch", path: "/literature/fetch", label: "Mass import", parent: "literature" },
  { id: "pubmed", path: "/literature/fetch/pubmed", label: "Fetch from PubMed", parent: "literature", source: "pubmed" },
  { id: "pmc", path: "/literature/fetch/pmc", label: "Fetch from PubMed Central", parent: "literature", source: "pmc" },
  { id: "europepmc", path: "/literature/fetch/europepmc", label: "Fetch from Europe PMC", parent: "literature", source: "europepmc" },
];

const ALL = [...NAV.flatMap((g) => g.items), ...HIDDEN];

export function routeById(id) {
  return ALL.find((r) => r.id === id) ?? null;
}

// Parents of a route, outermost first.
export function ancestors(route) {
  const out = [];
  for (let r = route && routeById(route.parent); r; r = routeById(r.parent)) out.unshift(r);
  return out;
}

export function findRoute(pathname) {
  // Ignore any ?query or #hash: links like /literature?run=3 still resolve to their page.
  const clean = pathname.split(/[?#]/)[0].replace(/\/+$/, "") || "/";
  return ALL.find((r) => r.path === clean) ?? null;
}
