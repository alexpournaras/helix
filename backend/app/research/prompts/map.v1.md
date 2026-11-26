Step: mapping. You link verified evidence claims to the research hypotheses for one person's health problem.

The material gives the case summary, the hypotheses (`key`, title, description) and a list of claims (`C{n}`)
with their population, design and evidence type.

For every claim return one item with its `claim` ID and `links`: one entry for each listed hypothesis the claim
bears on, with `direction` (`supports`, `contradicts`, or `neutral` when it is about the hypothesis but points
neither way), `strength` (`weak`, `moderate`, `strong`), `case_match` (`high`, `partial`, `low`, `unknown`: how
well the studied people match this case) and a short `reason`. Leave out hypotheses the claim says nothing about.
Judge from the claim as written; a claim about a different condition is not evidence for this one.
