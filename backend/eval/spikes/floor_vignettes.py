"""Held-out vignettes for the universal safety floor (PLAN 1B.1, ahead of the E3.7 triage benchmark).

Run from backend/:  .venv/Scripts/python -m eval.spikes.floor_vignettes
Synthetic, agent-written vignettes that were NOT used to write the rules' own examples. An emergency vignette
passes when an emergency rule fires or is unknown (so its rule-out question is asked); an everyday vignette
passes when no emergency rule fires. Set 1 was used to fix the rules after its first run; set 2 was written
after those fixes and measured once before any further change (see docs/spikes.md).
"""

from __future__ import annotations

from app.research.knowledge import loader
from app.research.knowledge.conditions import Case
from app.research.segment import split_user

SET1_EMERGENCY = [
    ("My dad has crushing pain in the middle of his chest and is grey and sweaty.", {}),
    ("I can't get my words out and my right hand went numb ten minutes ago.", {}),
    ("Worst headache ever, started like a bang while I was at the gym.", {}),
    ("My 6 week old feels very hot and won't feed.", {"age_months": 1.5}),
    ("After the bee sting her tongue is swelling.", {}),
    ("I want to end my life tonight.", {}),
    ("He drank some bleach by mistake.", {}),
    ("I'm 30 weeks pregnant and have heavy bleeding.", {"pregnant": True, "weeks_pregnant": 30}),
    ("My toddler is breathing really fast and grunting.", {"age_years": 2}),
    ("Mum, 85, fell and hit her head, she is on apixaban.", {"age_years": 85}),
    ("High fever and she has purple spots that don't fade.", {}),
    ("Sudden agony in my right testicle while asleep.", {}),
    ("I have had a fever and now I feel very confused.", {}),
    ("I suddenly can't hear anything in my left ear since this morning.", {}),
    ("There's a black curtain coming across the vision of my right eye.", {}),
    ("My sugar is 32 and I keep throwing up.", {}),
    ("I have back pain and I wet myself without noticing.", {}),
    ("The left side of his face has dropped and he is slurring.", {}),
    ("I have type 1 diabetes, my ketones are high.", {}),
    ("Pain in my right lower belly, my period is 2 weeks late.", {"sex": "female", "age_years": 24}),
]
SET1_EVERYDAY = [
    ("I have had a runny nose and a sore throat for two days.", {}),
    ("Mild lower back pain after lifting boxes, no other symptoms.", {}),
    ("I get heartburn after spicy food.", {}),
    ("My knee aches when it rains.", {"age_years": 45}),
    ("I have trouble falling asleep and wake up tired.", {}),
    ("Occasional ringing in both ears after loud music.", {}),
    ("I have dandruff and an itchy scalp.", {}),
    ("Tension headache after long days at the computer, no fever.", {}),
    ("My toddler has a mild cough but is playing and eating normally.", {"age_years": 2}),
    ("I feel a bit stressed at work lately.", {}),
    ("I had food poisoning last week and feel better now.", {}),
    ("I got my flu shot and my arm is sore.", {}),
    ("Constipation for a couple of days after travel.", {}),
    ("Dry skin on my hands in winter.", {}),
    ("I sneeze a lot in spring, hay fever.", {}),
]
SET2_EMERGENCY = [
    ("For the last hour there is a heavy weight on my chest and I feel sick.", {}),
    ("My wife's mouth is drooping on the left and she can't raise her arm.", {}),
    ("A sudden splitting headache hit me out of nowhere, worst pain I've felt.", {}),
    ("Our 2 month old has a temperature of 38.4.", {"age_months": 2}),
    ("My throat feels like it's closing after taking the new antibiotic.", {}),
    ("I keep thinking about killing myself.", {}),
    ("My son swallowed some of my sleeping pills.", {"age_years": 4}),
    ("I'm pregnant and I've been bleeding since this morning.", {"pregnant": True}),
    ("The baby is floppy and won't wake up properly.", {"age_months": 5}),
    ("Grandma tripped and banged her head, she takes warfarin.", {"age_years": 88}),
    ("He has a fever, is shivering badly and his skin looks blotchy.", {}),
    ("I can't pee at all since last night and it really hurts.", {}),
    ("My eye is agonisingly painful and red and my vision is blurred, I see halos.", {}),
    ("She had a seizure for the first time and is still very drowsy.", {}),
    ("My chest hurts and I'm struggling to breathe.", {}),
    ("I vomited what looked like coffee grounds.", {}),
    ("He's 7 months, breathing fast with his ribs pulling in.", {"age_months": 7}),
    ("I'm 26 weeks and my waters just broke.", {"pregnant": True, "weeks_pregnant": 26}),
    ("I have chemotherapy and my temperature is 38.6.", {"chemotherapy": True}),
    ("My friend says she took an overdose an hour ago.", {}),
]
SET2_EVERYDAY = [
    ("My nose has been blocked for a week with a cold.", {}),
    ("I have a mild headache when I skip coffee.", {}),
    ("A bit of indigestion after big dinners.", {}),
    ("My shoulder is stiff after sleeping on it badly.", {"age_years": 38}),
    ("I feel tired after night shifts.", {}),
    ("I have mild acne on my chin.", {}),
    ("My ears feel itchy sometimes.", {}),
    ("My 5 year old has a scraped knee from the playground.", {"age_years": 5}),
    ("I get a little out of breath when I sprint for the bus, then it settles.", {}),
    ("Some hair loss when I brush, which my mum also had.", {}),
    ("I had a cold sore on my lip last month.", {}),
    ("My periods are a bit irregular this year.", {"sex": "female", "age_years": 33}),
    ("I need reading glasses for small print now.", {"age_years": 47}),
    ("Muscle soreness two days after a new gym class.", {}),
    ("I worry a lot before exams.", {}),
]


def states(text: str, facts: dict) -> list[tuple[str, str, str]]:
    case = Case([(f"U{i}", s) for i, s in enumerate(split_user(text), 1)], facts)
    out = []
    for rule in loader.floor().rules:
        state = rule.check(case).state
        if state != "false":
            out.append((rule.id, rule.level, state))
    return out


def emergency_caught(text: str, facts: dict) -> bool:
    return any(level == "emergency" for _, level, _ in states(text, facts))


def false_emergency(text: str, facts: dict) -> bool:
    return any(level == "emergency" and state == "true" for _, level, state in states(text, facts))


def main() -> None:
    for name, emerg, everyday in (("set 1", SET1_EMERGENCY, SET1_EVERYDAY), ("set 2", SET2_EMERGENCY, SET2_EVERYDAY)):
        missed = [t for t, f in emerg if not emergency_caught(t, f)]
        fps = [t for t, f in everyday if false_emergency(t, f)]
        print(f"{name}: emergencies caught {len(emerg) - len(missed)}/{len(emerg)}; "
              f"everyday with a false emergency {len(fps)}/{len(everyday)}")
        for t in missed:
            print("  MISS", t)
        for t in fps:
            print("  FP  ", t, [i for i, lv, st in states(t, dict(next(f for x, f in everyday if x == t))) if lv == "emergency" and st == "true"])


if __name__ == "__main__":
    main()
