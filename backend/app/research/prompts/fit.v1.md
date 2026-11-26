Step: case fit. You judge how well one research hypothesis fits each feature of one person's case, using the
evidence claims given.

The material gives the hypothesis (title, description, mechanism, rating), the case features (`key` and text) and
up to 40 verified claims (`C{n}`) about the hypothesis.

Return `items`: exactly one per case feature, with:
- `fit`: `explains` (the hypothesis accounts for this feature, per the claims), `compatible` (consistent with it
  but doesn't explain it), `against` (the claims make this feature unlikely under the hypothesis), or `unknown`
  (the claims don't say).
- `claim_ids`: the claims that back the rating. `explains` and `against` need at least one.
- `reason`: one short sentence.
Use only the claims and the features given. Don't diagnose.
