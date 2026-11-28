 I read the full plan. My verdict: it is very strong and unusually detailed, but I would not call the token-budget design fully safe yet. The medical/research pipeline is well specified; the remaining
  weaknesses are mostly around enforcing the quota operationally.

  ## What is already strong

  The revised design makes sensible quality trade-offs:

  • Code validation remains mandatory.
  • Safety retains two reviews.
  • Rating-driving claims get a second opinion.
  • Grounding is reused rather than repeatedly purchased.
  • Screening, reading, and verification are bounded.
  • The report discloses skipped work.
  • incomplete and insufficient_evidence remain honest outcomes.
  • Evaluation measures the quality lost by moving away from the expensive profile.

  I agree with the overall direction. Ten carefully selected papers with targeted verification is preferable to trying to imitate the 38M-token quality profile and regularly failing halfway through.

  ## Must-fix issues

  ### 1. The 2.5M allowance is not actually a hard provider limit

  The wording in PLAN.md (/D:/Projects/helix/PLAN.md:1477) says it “can’t be exceeded.” According to OpenAI, it is a complimentary-token quota:

  • Usage beyond it is billed.
  • A request that crosses the boundary is billed in its entirety.
  • The counter resets at 00:00 UTC.
  • A positive account balance is still required.
  • The program can be terminated with 30 days’ notice.

  See the current OpenAI complimentary-token terms.

  I would change the terminology to:

  > “2.5M complimentary-token quota and Helix-enforced no-overage ceiling.”

  Helix—not the provider—must enforce the ceiling.

  ### 2. Per-investigation accounting does not enforce a daily account allowance

  Currently the plan sums usage by investigation and says to manually add usage from evaluation databases before a run. That cannot guarantee staying below 2.5M because:

  • Eight calls may be running concurrently.
  • Each checks the budget before starting.
  • A running call is allowed to finish.
  • Retries and repair calls consume additional tokens.
  • Other Helix databases may use the same project.
  • Another application or manual API request may use the project.

  Add a global, atomic daily quota ledger, separate from investigation databases:

  provider_quota_day
  - provider/account or organization
  - project
  - model_group
  - quota_date_utc
  - actual_input
  - actual_output
  - reserved_input
  - reserved_output
  - external_adjustment
  - reconciled_at

  Before every provider call:

  1. Calculate input tokens accurately.
  2. Reserve input plus the request’s permitted maximum output.
  3. Atomically reject or delay the call if all concurrent reservations would cross the ceiling.
  4. Replace the reservation with provider-reported actual usage afterward.
  5. Release it on a zero-usage failure.

  Without in-flight reservations, concurrency.openai: 8 can overshoot the remaining 100k buffer very easily.

  I would make this a new 1R.0 Daily quota controller, before the existing 1R tasks.

  ### 3. Free tokens and real health cases are incompatible under the current privacy policy

  This is presented as an open question in PLAN.md (/D:/Projects/helix/PLAN.md:18), but the answer is effectively known: complimentary tokens apply to traffic shared with OpenAI, and OpenAI says not to include
  sensitive information in shared traffic. Real medical cases are sensitive.

  I recommend two explicit operating modes:

   Mode                Cases                                          Data sharing    Budget
  ━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   shared_synthetic    Synthetic and licensed benchmark cases only    Enabled         2.5M complimentary ceiling
  ──────────────────  ─────────────────────────────────────────────  ──────────────  ──────────────────────────────────────────────
   private_real        Real health cases                              Disabled        Paid, with a separate monetary/token ceiling

  The existing rule “until then, only synthetic cases run” is correct. I would make it a permanent architectural separation rather than a temporary open question.

  ### 4. The completion reserve is slightly inconsistent

  The acquisition stop reserves 20% of 2.4M, which is 480k. But assess + report are budgeted at 500k in B17.4 (/D:/Projects/helix/PLAN.md:1572). The plan expects acquisition normally to finish earlier, but
  that is a target, not a guarantee.

  Also, non-acquisition groups are allowed to exceed their shares. Therefore, “the remaining 0.48M always finishes” is too strong.

  I would:

  • Reserve at least the measured P95 cost of assess + report, initially perhaps 650k.
  • Maintain a separate, untouchable finalization reserve.
  • Provide three finalization levels:
      1. Full checked report.
      2. Compact checked report using fewer claims and no prose revision.
      3. Deterministic incomplete record assembled by code without another model call.

  The report gate should require a valid completed report, not merely total usage below 2.4M.

  ### 5. Multiple new complaints do not fit the grounding calculation

  The inherited setting allows up to five complaints, but the grounding share is 400k total and the estimate is approximately 200k per complaint. A complex case containing three to five unrelated complaints
  could exhaust grounding before research begins.

  Define an explicit policy, for example:

  • Ground the chief complaint first.
  • Ground a second complaint only if it materially changes safety or the differential.
  • Use cached/curated knowledge for the remainder.
  • Otherwise record a grounding gap and ask the user to separate unrelated complaints into another investigation.

  The UI should tell the user when a case is too broad for one daily investigation.

  ### 6. The budget rework is not validated end-to-end

  Task 1R is declared done after a live run only to gaps in PLAN.md (/D:/Projects/helix/PLAN.md:1864), because report generation is implemented afterward in 1F. Therefore 1R cannot prove the headline promise
  “one case runs to its report.”

  Add a post-1F task:

  > 1R.6 End-to-end quota validation: run one new-grounding case and one reused-grounding case through finalization; record actual, reserved, retry, cached, and stage-group tokens.

  This should be mandatory before the M1 gate.

  ## Important additions

  ### Retry and failure allowance

  Repairs, truncation retries, task retries, and adjudications are not visibly included in the stage estimates. Add:

  • A dedicated retry contingency, perhaps 100–150k.
  • Per-task maximum attempts and maximum tokens.
  • “Do not repair/retry if doing so would invade the finalization reserve.”
  • Fault-injection evaluation proving that a late malformed response cannot destroy report completion.

  ### Grounding-cache correctness

  Caching a gap for 180 days is risky if the gap resulted from an API outage, parsing failure, or poor query. Only cache a gap when all configured sources were successfully searched.

  The cache key should also include relevant variables such as:

  • Jurisdiction or guideline region.
  • Age band, not just a broad population label.
  • Pregnancy/postpartum status.
  • Immunocompromised status.
  • Search-source and query versions.
  • Language.
  • Search date/freshness policy.

  Operational failures should have a short negative-cache lifetime, not 180 days.

  ### Pin the model snapshot

  For evaluation reproducibility, use gpt-5.4-mini-2026-03-17, while allowing the alias only in development. The current model supports the planned effort levels, but aliases can change behavior. Current
  prices are $0.75/M input and $4.50/M output, so the table’s “about $3/M mixed” should be labelled a measured blended estimate rather than a model price. See the official GPT-5.4 mini documentation.

  ### Define the quota day explicitly

  The allowance resets at 00:00 UTC, which is 02:00 in Athens during winter and 03:00 during summer. Display:

  • Current UTC quota day.
  • Used, reserved, and remaining tokens.
  • Next reset time in local time.
  • Whether a case can safely start.
  • Whether it is paused until reset.

  A case crossing midnight UTC should keep correct per-request accounting rather than being assigned wholly to its start date.

  ### Narrow the release claim, even if the engine remains universal

  “Any human health problem” is much broader than what 20 full investigations across 10 systems can establish. Readiness labels are helpful, but merely displaying “untested” does not reduce clinical risk.

  My preferred policy:

  • The engine may accept every health problem.
  • tested areas may receive the full findings report.
  • limited areas receive stronger warnings.
  • untested areas receive safety review and an evidence inventory, but no ranked explanations until the relevant evaluation gate passes.

  If you want to retain full reports for untested systems, the plan should at least classify that explicitly as experimental research rather than validated functionality.

  ## Smaller corrections

  • The title says v8, while the revision history contains v8.1.
  • “One case per day” should mean one case per UTC quota day, not local calendar day.
  • “Cost about $6” should be “list-price equivalent/possible overage”; eligible shared synthetic traffic within the quota costs no token charge.
  • The reported 1–2 hour target conflicts with max_wall_minutes: 240; add median and P95 wall-time gates.
  • B8 says final_check uses a different model family, while budget uses the same OpenAI family. Add a Budget note there so the reader does not have to rely solely on the B17 precedence rule.
  • Add a program-change fallback because complimentary tokens are promotional rather than permanent.

  ## Overall opinion

  I would approve the revised research strategy, but not yet its quota enforcement. The biggest required changes are:

  1. A global UTC daily quota controller with atomic in-flight reservations.
  2. Permanent separation of synthetic/free and real/private modes.
  3. A larger, measured finalization reserve.
  4. Explicit handling of multi-complaint cases.
  5. A full end-to-end budget test after report generation is implemented.

  With those changes, the plan becomes operationally credible for at least one complete synthetic investigation per quota day. Without them, it can stay under 2.4M in ordinary tests yet still produce a billed
  request or run out of quota before the report.