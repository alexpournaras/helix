import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { ChevronRight, Menu, X } from "lucide-react";
import { api } from "./api.js";
import { NAV, ancestors, findRoute } from "./routes.js";
import { Logo } from "./components/Logo.jsx";
import { Dashboard } from "./views/Dashboard.jsx";
import { Integrations } from "./views/Integrations.jsx";
import { Literature } from "./views/Literature.jsx";
import { LiteratureFetch } from "./views/LiteratureFetch.jsx";
import { LiteratureSource } from "./views/LiteratureSource.jsx";
import { OllamaSettings } from "./views/OllamaSettings.jsx";
import { Settings } from "./views/Settings.jsx";
import { Placeholder } from "./views/Placeholder.jsx";
import { NotFound } from "./views/NotFound.jsx";

function usePath() {
  const [path, setPath] = useState(() => window.location.pathname);
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const navigate = useCallback((to) => {
    if (to !== window.location.pathname) {
      window.history.pushState(null, "", to);
      setPath(to);
    }
    document.getElementById("main")?.scrollTo(0, 0);
  }, []);
  return [path, navigate];
}

const isModifiedClick = (e) => e.button !== 0 || e.metaKey || e.altKey || e.ctrlKey || e.shiftKey;

export default function App() {
  const [path, navigate] = usePath();
  const [settings, setSettings] = useState(null);
  const [apiOnline, setApiOnline] = useState(null);
  const [navOpen, setNavOpen] = useState(false);

  const loadSettings = useCallback(() => {
    setApiOnline(null);
    api.getSettings()
      .then((s) => { setSettings(s); setApiOnline(true); })
      .catch(() => setApiOnline(false));
  }, []);
  useEffect(loadSettings, [loadSettings]);

  const route = findRoute(path);
  const trail = useMemo(() => ancestors(route), [route]);
  useEffect(() => {
    document.title = route && route.id !== "dashboard" ? `${route.label} – Helix` : "Helix";
    setNavOpen(false);
  }, [route]);

  const link = (to) => ({
    href: to,
    onClick: (e) => {
      if (isModifiedClick(e)) return;
      e.preventDefault();
      navigate(to);
    },
  });

  let view;
  if (!route) view = <NotFound link={link} />;
  else if (route.id === "dashboard") view = <Dashboard settings={settings} apiOnline={apiOnline} link={link} onRetry={loadSettings} />;
  else if (route.id === "integrations") view = <Integrations apiOnline={apiOnline} link={link} />;
  else if (route.id === "ollama") view = <OllamaSettings apiOnline={apiOnline} link={link} />;
  else if (route.source) view = <LiteratureSource key={route.source} source={route.source} apiOnline={apiOnline} link={link} />;
  else if (route.id === "literature") view = <Literature apiOnline={apiOnline} link={link} />;
  else if (route.id === "literature-fetch") view = <LiteratureFetch apiOnline={apiOnline} link={link} />;
  else if (route.id === "settings") view = <Settings settings={settings} onSaved={setSettings} apiOnline={apiOnline} onRetry={loadSettings} />;
  else view = <Placeholder route={route} link={link} />;

  const status = apiOnline ? ["on", "Backend running"] : apiOnline === false ? ["off", "Backend offline"] : ["wait", "Connecting"];

  return (
    <div className="shell">
      <a className="skip" href="#main">Skip to content</a>

      <aside className={"sidebar" + (navOpen ? " is-open" : "")}>
        <div className="sidebar-top">
          <a className="brand" {...link("/")}>
            <Logo />
            <span className="brand-name">Helix</span>
          </a>
          <button className="icon-btn only-mobile" onClick={() => setNavOpen(false)} aria-label="Close menu">
            <X size={16} />
          </button>
        </div>

        <nav className="sidebar-nav" aria-label="Main">
          {NAV.map((group, gi) => (
            <div className="nav-group" key={gi}>
              {group.section && <div className="nav-heading">{group.section}</div>}
              {group.items.map(({ id, path: to, label, icon: Icon, soon }) => (
                <a key={id} className="nav-item" aria-current={route?.id === id || trail.some((r) => r.id === id) ? "page" : undefined} {...link(to)}>
                  <Icon size={16} strokeWidth={1.75} />
                  <span>{label}</span>
                  {soon && <span className="nav-soon">Soon</span>}
                </a>
              ))}
            </div>
          ))}
        </nav>

        <a className="sidebar-foot" {...link("/settings")} title="Workspace settings">
          <div className="avatar">{(settings?.workspace_name || "H").slice(0, 1)}</div>
          <div className="foot-meta">
            <div className="foot-name">{settings?.workspace_name || "Helix"}</div>
            <div className="foot-sub">{settings?.organization || "Administrator"}</div>
          </div>
          <ChevronRight size={14} className="foot-chevron" aria-hidden="true" />
        </a>
      </aside>
      {navOpen && <div className="scrim" onClick={() => setNavOpen(false)} />}

      <div className="main">
        <header className="topbar">
          <button className="icon-btn only-mobile" onClick={() => setNavOpen(true)} aria-label="Open menu">
            <Menu size={16} />
          </button>
          {route?.parent ? (
            <nav className="crumbs" aria-label="Breadcrumb">
              {trail.map((r) => (
                <Fragment key={r.id}>
                  <a {...link(r.path)}>{r.label}</a>
                  <ChevronRight size={14} aria-hidden="true" />
                </Fragment>
              ))}
              <h1 className="topbar-title">{route.label}</h1>
            </nav>
          ) : (
            <h1 className="topbar-title">{route?.label ?? "Not found"}</h1>
          )}
          <span className="status" data-state={status[0]}>
            <i aria-hidden="true" />
            {status[1]}
          </span>
        </header>
        <main id="main" className="content">
          <div className="content-inner" key={route?.id ?? "404"}>{view}</div>
        </main>
      </div>
    </div>
  );
}
