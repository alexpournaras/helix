Step: whole-paper claim extraction. Others have extracted claims from each part of this paper separately. You
read the whole paper and add the claims that need context from several sections (e.g. a result that only
makes sense with the population described in the methods, or a conclusion qualified in the discussion).

The material gives the case summary, the hypotheses, the paper's design, the claims already extracted, and the
whole paper with sentence, caption and cell IDs.

Return only new `claims`, in the same shape as the per-part extraction (1 to 4 `sentence_ids` from anywhere in
the paper, `evidence_type`, `population`, `design`, `n`, `effect` as written, `limitations`, `links`). Don't
repeat a claim already extracted. Return an empty list if nothing is missing.
