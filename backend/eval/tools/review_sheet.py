"""Clinician review sheet for the universal floor, the curated packs and the fixed safety texts (PLAN Part G.6).

    .venv/Scripts/python -m eval.tools.review_sheet [--out ../docs/review/knowledge-review.md]

The YAML files are written for code (regex patterns); this renders what a clinician needs to judge each rule:
title, level, population, rule-out question, the example sentences that must and must not trigger it, and the
sources. Regenerate after every change to the knowledge files; the sheet records the versions it was made from.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from app.research import safety_rules as R
from app.research.knowledge import loader

LEVEL_LABEL = {"emergency": "EMERGENCY", "urgent": "Urgent", "routine_review": "Routine review"}
POP_LABEL = {"all": "Everyone", "infant": "Infants (< 1)", "child": "Children (< 16)", "pregnancy": "Pregnancy",
             "older": "Older adults (≥ 65)", "immunocompromised": "Immunocompromised"}
AREA_LABEL = {"heart_lungs_allergy": "Heart, lungs, allergy", "brain_nerves": "Brain and nerves",
              "infection_sepsis": "Infection, sepsis", "abdomen_gut": "Abdomen, gut",
              "urinary_genital": "Urinary, genital", "eye": "Eye", "ent": "Ear, nose, throat", "skin": "Skin",
              "hormones_metabolism": "Hormones, metabolism", "mental_health": "Mental health",
              "poisoning_injury": "Poisoning, injury", "pregnancy": "Pregnancy", "children": "Children",
              "older_adults": "Older adults", "possible_cancer": "Possible cancer"}
BOX = "- [ ] Level right\n- [ ] Population right\n- [ ] Question clear\n- [ ] Examples right\n\nComments:"

# A filled-in review of an invented rule, so reviewers see what a useful review looks like. Dummy data only:
# the rule, the reviewer and the comments are made up and are not part of the floor.
EXAMPLE = [
    "## Example: a completed review (dummy data)", "",
    "> **This is an invented rule with a made-up review, shown only as an example.** It is not part of Helix's "
    "rules. The real rules start in section 1.", "",
    "#### Toothache with facial swelling", "",
    "`rule:example.toothache_swelling` · **Urgent** · Everyone", "",
    "**Question when information is missing:** Is your face or jaw swollen, or do you have a fever?", "",
    "**Should trigger it:**",
    "- “My tooth has hurt for two days and now my cheek is swollen.”",
    "- “Bad toothache and my jaw is puffy and hot.”", "",
    "**Should not trigger it:**",
    "- “My tooth is sensitive to cold drinks.”",
    "- “Toothache, no swelling, no fever.”", "",
    "**Sources:** NHS: Dental abscess (example)", "",
    "- [x] Level right", "- [ ] Population right", "- [x] Question clear", "- [ ] Examples right", "",
    "Comments: _(Reviewer A, GP, 2026-12-01, example)_", "",
    "- **Population:** in children under 5, facial swelling with fever should be **emergency**, not urgent. "
    "Suggest a separate child rule.",
    "- **Escalation missing:** swelling that spreads to the eye or neck, or difficulty swallowing or breathing, "
    "is an **emergency** at any age. Add as a separate emergency rule.",
    "- **Examples:** add a sentence that should trigger it without the word “swollen”, e.g. “my face has "
    "puffed up on one side”.",
    "- Question is fine.", "",
    "What makes this review useful: each comment names **what** is wrong (level, population, missing rule, "
    "examples), **why**, and **what to change**. A short “looks fine” is also useful when a rule is right.", "",
    "---", "",
]


def _facts(facts: dict) -> str:
    return f" _(facts: {', '.join(f'{k} = {v}' for k, v in facts.items())})_" if facts else ""


def _sources(ids: list[str], sources: dict) -> str:
    out = []
    for sid in ids:
        s = sources.get(sid)
        out.append(f"[{s.publisher}: {s.title}]({s.url})" if s else sid)
    return "; ".join(out)


def _rule(r, sources: dict, ref: str) -> list[str]:
    lines = [f"#### {r.title}", "",
             f"`{ref}` · **{LEVEL_LABEL[r.level]}** · {POP_LABEL.get(r.population, r.population)}"
             + (" · **shows crisis resources**" if getattr(r, "crisis", False) else ""), "",
             f"**Question when information is missing:** {r.rule_out_question}", ""]
    lines.append("**Should trigger it:**")
    lines += [f"- “{e.text}”{_facts(e.facts)}" for e in r.examples.fires]
    lines.append("")
    lines.append("**Should not trigger it:**")
    lines += [f"- “{e.text}”{_facts(e.facts)}" for e in r.examples.no_fire]
    if r.examples.unknown:
        lines += ["", "**Should ask the question (not enough information):**"]
        lines += [f"- “{e.text}”{_facts(e.facts)}" for e in r.examples.unknown]
    if r.notes:
        lines += ["", f"_Note: {r.notes}_"]
    lines += ["", f"**Sources:** {_sources(r.sources, sources)}", "", BOX, "", "---", ""]
    return lines


def render() -> str:
    floor = loader.floor()
    packs = loader.load_packs()
    sources = loader.load_sources()
    counts = {lvl: sum(r.level == lvl for r in floor.rules) for lvl in LEVEL_LABEL}
    out = [
        "# Helix knowledge review sheet", "",
        f"Generated {date.today().isoformat()} from the universal floor v{floor.version} and "
        + ", ".join(f"the {p.complaint} pack v{p.version}" for p in packs.values())
        + ". Regenerate with `python -m eval.tools.review_sheet` after any change.", "",
        "## How to review", "",
        "These rules decide when Helix tells someone to seek care and stops researching. They were **drafted by an "
        "AI agent** from NHS and NICE pages and are marked unreviewed until two reviewers, ideally clinicians, have "
        "checked them (PLAN Part G.6).", "",
        "For each rule, please check:",
        "1. **Level**: emergency (call the emergency number / go to A&E now), urgent (a clinician within days), or "
        "routine review (mention at a routine appointment).",
        "2. **Population**: does the rule apply to the right people?",
        "3. **Question**: is the question asked when information is missing clear and safe?",
        "4. **Examples**: should the “trigger” sentences really trigger it, and the others not?",
        "5. **Missing rules**: anything a triage nurse would always ask about that isn't here?", "",
        "When the rules err, they should err towards asking or escalating. Write comments under each rule, or "
        "mark the file and send it back.", "",
        *EXAMPLE,
        f"**Universal floor:** {len(floor.rules)} rules ({counts['emergency']} emergency, {counts['urgent']} urgent, "
        f"{counts['routine_review']} routine review).", "",
        "## 1. Fixed texts", "",
        "Shown word for word; no AI text is added to the emergency notice.", "",
    ]
    for level in ("emergency", "urgent", "routine_review"):
        out += [f"**{LEVEL_LABEL[level]}:**", "", f"> {R.TEMPLATES[level]}", ""]
    out += ["**Crisis resources** (shown immediately for thoughts of suicide or self-harm). "
            "Please check every phone number:", ""]
    out += ["> " + line if line else ">" for line in R.CRISIS_RESOURCES.split("\n")]
    out += ["", f"**When research stops:** {R.STOPPED}", "",
            "- [ ] Wording clear and safe\n- [ ] Every phone number correct\n\nComments:", "", "---", "",
            "## 2. Universal safety floor", ""]
    by_area: dict[str, list] = {}
    for r in floor.rules:
        by_area.setdefault(r.area, []).append(r)
    for area in loader.AREAS:
        rules = by_area.get(area)
        if not rules:
            continue
        rules.sort(key=lambda r: -loader.LEVEL_RANK[r.level])
        out += [f"### {AREA_LABEL.get(area, area)} ({len(rules)})", ""]
        for r in rules:
            out += _rule(r, sources, f"rule:{r.id}")
    for p in packs.values():
        out += [f"## 3. {p.complaint.title()} pack", "",
                f"Built by: {p.built_by or 'hand'}. MeSH: {', '.join(p.mesh)}.", "", "### Red flags", ""]
        for r in p.red_flags:
            out += _rule(r, sources, f"pack:{p.complaint}:{r.id}")
        out += ["### Causes to consider", "", "| Cause | Key features | Questions that tell them apart | "
                "Usual tests | Red flag | Sources |", "|---|---|---|---|---|---|"]
        for e in p.etiologies:
            out.append(f"| {e.name} | {'; '.join(e.key_features)} | {'; '.join(e.discriminating_questions)} | "
                       f"{'; '.join(e.typical_evaluations)} | {'yes' if e.red_flag else ''} | "
                       f"{_sources(e.sources, sources)} |")
        out += ["", "### History questions", "", "| Priority | Question | Sources |", "|---|---|---|"]
        out += [f"| {h.priority} | {h.question} | {_sources(h.sources, sources)} |" for h in p.history_checklist]
        out += ["", "### Tests and examinations", "", "| Test | When | Sources |", "|---|---|---|"]
        out += [f"| {e.test} | {e.indication} | {_sources(e.sources, sources)} |" for e in p.evaluations]
        out += ["", "### Management topics (to discuss with a clinician)", "", "| Topic | Context | Sources |",
                "|---|---|---|"]
        out += [f"| {m.topic} | {m.context} | {_sources(m.sources, sources)} |" for m in p.management_topics]
        out += ["", "Pack comments:", "", "---", ""]
    out += ["## Sign-off", "", "| Reviewer | Role | Date | Files reviewed | Overall: approve / changes needed |",
            "|---|---|---|---|---|", "| | | | | |", "| | | | | |", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default = Path(__file__).resolve().parents[3] / "docs" / "review" / "knowledge-review.md"
    parser.add_argument("--out", default=str(default))
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(), "utf-8")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
