# Field lengths: aligning the schema, the store, and the surfaces

Written 2026-09-12. The question this answers: are the field lengths an agent
sends the right lengths for what the UI shows?

**They were not aligned.** Each cap and each surface was chosen independently,
and nothing connected them. This document records the measured numbers, the one
latent defect the gap created, and the budgets that now link the layers.

## The three layers, measured

### What the schema accepts (MCP `inputSchema`)

| Field | Cap | Why it was chosen |
| --- | --- | --- |
| `summary`, `blocker`, `next_step`, `resolution_summary` | 1,200 | not documented anywhere |
| `project_dir` | 1,000 | a path |
| `command`, `artifact_path`, `artifact_url` | 500 | not documented |
| `artifact_ref`, `idempotency_key`, session/turn ids | 240 | not documented |
| `section_title` / `title` | 160 | not documented |
| `section_id`, `work_id` | 120 | not documented |
| `source`, `client`, `phase`, `provider`, `model` | 80 | not documented |

Plus a **shared 8,192-byte metadata budget**: `summary` + `blocker` +
`next_step` at their individual caps cannot coexist, so the effective limit is
the byte budget, not the per-field one. That interaction is real and was already
handled (`_metadata_size_error` names the largest field to shrink), but an author
reading only the field caps cannot predict it.

### What the store holds (measured, 538 work items / 820 summaries)

| Field | Median | p90 | Max | At cap |
| --- | --- | --- | --- | --- |
| `section_title` | 40 | 52 | 77 | 0 |
| `summary` | 27 words / 207 chars | 396 | 1,024 | 0 |

So agents naturally write well inside the caps. The caps are not the binding
constraint; the **surfaces** are.

### What the surfaces render

| Surface | Room | Source |
| --- | --- | --- |
| Canvas card title | ~54 chars, 2 lines | 200 pt card, 6 pt padding, 14 pt `.rowLabel`, ~0.55 em advance (`WorkTimeCanvas.swift:200`, `WorkTimeCanvasLayout.swift:61`, `Theme.swift:319`) |
| Canvas card body | title, lane and result only — **no summary** | `WorkTimeCanvas.swift:195-202` |
| Inspector | the full summary, unlimited | `WorkTimelineView.swift:568` |
| TUI check row | summary inlined in ONE line | `tui.py:2053` |
| Receipt Markdown cell | a phrase beside other columns | `receipt_markdown.py` |

## The defect the gap created

`task_timeline.py` built a card title as
`item.get("title") or item.get("summary")`. A section with no title therefore
handed its **1,200-character summary** to a surface that renders two lines of
14 pt text, and the reader saw an arbitrary slice of a paragraph with nothing
indicating there was more. The same fallback existed in the session-title map.

**Honest status: it had not fired.** Zero of 538 work items took that path, so
this was a latent defect, not a live one. It is fixed anyway, because it is one
missing field away from firing and its failure is silent.

## What now links the layers

`src/agentacct/display_budget.py` holds one budget per surface, each with the
derivation recorded in a comment:

| Budget | Value | Surface |
| --- | --- | --- |
| `CARD_TITLE_CHARACTERS` | 54 | canvas card label |
| `CARD_FALLBACK_TITLE_CHARACTERS` | 54 | a summary reused as a label |
| `INSPECTOR_SUMMARY_CHARACTERS` | 600 | the inspector's reading budget |
| `TERMINAL_LINE_CHARACTERS` | 150 | single-line rows |
| `MARKDOWN_CELL_CHARACTERS` | 80 | exported table cells |

They are **display budgets, not validation limits**. The schema keeps its
generous caps, because one field feeds several surfaces; what changed is that a
surface no longer invents its own truncation, and the schema now *tells the
author* how much of the field a reader sees.

Changes that follow from it:

1. **The label fallback is a sentence, not a clip.** Prose reused as a title is
   reduced to its first sentence and cut at a word boundary within the card
   budget.
2. **The receipt's objective cell is bounded** — it joined up to two full
   objective strings into one table cell.
3. **The schema descriptions state the budget**: `section_title` says a card
   renders about 54 characters and to put the distinguishing words first;
   `summary` says the inspector renders about 600 before a summary becomes a
   report. An author cannot respect a budget they are not told about.

## Measured effect on real data

| | Before | After |
| --- | --- | --- |
| Card titles longer than the card can show | 28 of 538 (5.2%) | unchanged — the cap is 160 and the card is 54, so a long-but-real title still clips at the card. The full title remains in the inspector and on hover |
| Cards showing a clipped paragraph as their title | 0 (path was latent) | 0, and the path now yields a sentence |
| Receipt objective cells | up to 2 full objectives | ≤ 80 characters |

The 5.2% is worth naming rather than hiding: a title between 54 and 160
characters is stored in full and shown truncated on the card. That is a
deliberate trade — the inspector and the hover tooltip both have the full text —
but it is the one place where the schema and the card still disagree, and
tightening the schema cap would be the way to close it if review shows those
titles reading badly.

## What I am not claiming

- The 54-character card estimate is **derived from geometry, not measured on
  screen**. It assumes a ~0.55 em average glyph advance at 14 pt proportional
  semibold. A pixel measurement of a rendered card would replace the estimate
  with a fact; until then, treat 54 as ±10%.
- I did not visually inspect the running app. The surface limits come from
  reading the view code and the font table.
- The budgets are enforced at the display boundary and disclosed in the schema,
  not enforced at write time. A future tightening of the schema caps should wait
  for a review of how the 28 over-budget titles actually read.
