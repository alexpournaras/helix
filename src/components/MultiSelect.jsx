import { useEffect, useId, useRef, useState } from "react";
import { Check, ChevronDown, Search } from "lucide-react";

/**
 * A filter dropdown that allows several choices. Nothing picked means "all".
 * options: [{ value, label, dot?, count? }]. Long lists get a search box.
 */
export function MultiSelect({ label, allLabel, options, value, onChange, noun = "selected", emptyText = "Nothing to choose yet" }) {
  const [open, setOpen] = useState(false);
  const [find, setFind] = useState("");
  const wrapRef = useRef(null);
  const buttonRef = useRef(null);
  const listId = useId();

  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => { if (!wrapRef.current?.contains(e.target)) setOpen(false); };
    const onKey = (e) => {
      if (e.key === "Escape") { setOpen(false); buttonRef.current?.focus(); }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    wrapRef.current?.querySelector(".multi-panel input")?.focus();
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  useEffect(() => { if (!open) setFind(""); }, [open]);

  const chosen = new Set(value);
  const toggle = (v) => onChange(chosen.has(v) ? value.filter((x) => x !== v) : [...value, v]);

  // Keep picked values that no longer exist as options, so they can still be unticked.
  const all = [...options, ...value.filter((v) => !options.some((o) => o.value === v)).map((v) => ({ value: v, label: v }))];
  const needle = find.trim().toLowerCase();
  const shown = needle ? all.filter((o) => o.label.toLowerCase().includes(needle)) : all;

  let summary = allLabel;
  if (value.length === 1) summary = all.find((o) => o.value === value[0])?.label ?? value[0];
  else if (value.length > 1) summary = `${value.length} ${noun}`;

  return (
    <div className="menu-wrap multi" ref={wrapRef}>
      <button
        ref={buttonRef}
        type="button"
        className={`input multi-button${value.length ? " is-active" : ""}`}
        aria-haspopup="true"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        onClick={() => setOpen(!open)}
      >
        <span className="sr-only">{label}: </span>
        <span className="multi-summary">{summary}</span>
        <ChevronDown size={14} className="menu-caret" aria-hidden="true" />
      </button>
      {open && (
        <div className="menu multi-panel" id={listId} role="group" aria-label={label}>
          {all.length > 8 && (
            <label className="multi-find">
              <Search size={13} aria-hidden="true" />
              <input value={find} onChange={(e) => setFind(e.target.value)} placeholder={`Find ${label.toLowerCase()}`} aria-label={`Find ${label.toLowerCase()}`} />
            </label>
          )}
          <ul className="multi-list">
            {shown.map((o) => (
              <li key={o.value}>
                <label className="multi-option">
                  <input type="checkbox" className="sr-only" checked={chosen.has(o.value)} onChange={() => toggle(o.value)} />
                  <span className="multi-box" aria-hidden="true"><Check size={11} strokeWidth={3} /></span>
                  {o.dot && <span className={`chip-dot ${o.dot}`} aria-hidden="true" />}
                  <span className="multi-label">{o.label}</span>
                  {o.count != null && <span className="multi-count">{o.count}</span>}
                </label>
              </li>
            ))}
            {!shown.length && <li className="multi-empty">{all.length ? "No matches" : emptyText}</li>}
          </ul>
          {value.length > 0 && (
            <div className="multi-foot">
              <button type="button" className="link small" onClick={() => onChange([])}>Clear</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
