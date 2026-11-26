Step: paper profile. You describe one research paper's study from its text.

The material gives the paper text: every sentence, caption or table cell has an ID in square brackets.

Return every field as `{value, sentence_ids}`: a short English value as the paper states it (null when the text
doesn't say) and 1 to 4 IDs that state it. Fields: `question`, `design`, `setting`, `population`,
`inclusion_criteria`, `n` (the number analysed, as written), `exposure`, `comparator`, `outcomes`, `follow_up`,
`funding`, `conflicts`, `review_included_designs` (reviews only: the study designs they included),
`dataset_or_cohort` (a named cohort, trial, registry or dataset the data come from), `secondary_analysis`
("yes" when the paper analyses data published elsewhere, else "no").
Only what the text states. Never cite an ID that isn't shown.
