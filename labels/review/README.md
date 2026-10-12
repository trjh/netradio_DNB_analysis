# labels/review/ — the align tool's semi-manual annotations

`<stem>.origNNN.review.tsv` — machine context rows plus hand notations made in the
align review surfaces (the align server's `/align/review` page and the Mac review
app). **Never regenerated**: these carry hand judgement, which is why they are in
git. Written by the align server's review-note route and auto-committed to the
`align-review-data` branch by `review_sync`; they reach the authoritative
`labels/*.labels.tsv` only through the manual fold.
