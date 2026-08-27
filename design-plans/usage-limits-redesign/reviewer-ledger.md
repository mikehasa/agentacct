# Usage and limits reviewer ledger

The runtime permits four concurrent agents and imposes a cumulative thread
ceiling. It stopped accepting new identities after 16 distinct scenario
reviewers had contributed. This ledger records that constraint explicitly; it
does not preserve the coordinator's never-created pending paths.

Two additional agents served as design critic and review coordinator:
`/root/critique_a` and `/root/critique_b`.

| Scenarios | Canonical reviewer | Contribution |
| --- | --- | --- |
| 001 | `/root/review_001` | original review |
| 002 | `/root/review_002` | original review |
| 003 | `/root/critique_b/review_003` | original review |
| 004 | `/root/critique_b/review_003/review_004` | original review |
| 005, 015-039 | `/root/critique_b/review_003/review_004/review_005` | original review plus simulated block |
| 006 | `/root/critique_b/review_003/review_004/review_005/review_006` | original review |
| 007 | `/root/critique_b/review_003/review_004/review_005/review_006/review_007` | original review |
| 008, 070-100 | `/root/critique_b/review_003/review_008` | original review plus simulated block |
| 009, 040-069 | `/root/critique_b/review_003/review_004/review_009` | original review plus simulated block; condensed 052/053 |
| 010 | `/root/critique_b/review_003/review_004/review_005/review_010` | original review |
| 011 | `/root/critique_b/review_003/review_004/review_005/review_011` | original review |
| 012 | `/root/critique_b/review_003/review_004/review_005/review_012` | original review |
| 013 | `/root/critique_b/review_003/review_004/review_005/review_006/review_013` | original review |
| 014 | `/root/critique_b/review_003/review_008/review_014` | original review |
| 052 | `/root/critique_b/review_003/review_052` | original review, later condensed by reviewer 009 |
| 053 | `/root/critique_b/review_003/review_052/review_053` | original review, later condensed by reviewer 009 |

Final corpus: 100 numbered scenario files, 16 scenario-reviewer identities,
and 18 non-root design agents in total. Correlation is highest inside the three
simulated blocks and is treated as such in `review-synthesis-input.md`.
