import { Bot, BookOpen, Database, FolderKanban, LayoutDashboard, Plug, Settings } from "lucide-react";

// `blurb` explains a not-yet-built section in plain terms.
export const NAV = [
  {
    items: [{ id: "dashboard", path: "/", label: "Dashboard", icon: LayoutDashboard }],
  },
  {
    section: "Research",
    items: [
      {
        id: "projects", path: "/projects", label: "Projects", icon: FolderKanban, soon: true,
        blurb: "A project gathers the patients, datasets, papers and AI runs behind one research question.",
      },
      {
        id: "literature", path: "/literature", label: "Literature", icon: BookOpen, soon: true,
        blurb: "Search PubMed and other sources, save papers to a project and annotate them.",
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

const ALL = NAV.flatMap((g) => g.items);

export function findRoute(pathname) {
  const clean = pathname.replace(/\/+$/, "") || "/";
  return ALL.find((r) => r.path === clean) ?? null;
}
