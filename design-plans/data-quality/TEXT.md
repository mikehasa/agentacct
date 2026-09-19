# Information text: what agents write, how it renders, and how to regulate it

Companion to [FINDINGS.md](FINDINGS.md) (structure and enforcement) and
[RULES.md](RULES.md) (the rule catalogue). This document covers the *prose*: the
text data agents record, the copy the surfaces invent, and the mismatch between
them. Measurements are from the installed ledger (8,369 events, 816 recorded
summaries, 1,166 titles) and from the write path.

## 1. The text data we actually receive

Measured over the 816 recorded summaries and 1,166 section titles:

| Property | Measured |
| --- | --- |
| Summary length | 27 words median, 11 words p10, 45 words p90; 207 characters median, 1024 max |
| Summary shape | 2 sentences median, 4 at p90 |
| Line breaks in summaries | **0%** |
| Bullet or numbered lists | **0%** |
| Markdown (backticks, `**`, headings, tables) | **0%** |
| Contains a number | 62% |
| Names a file | 6% |
| Contains a URL | 0.9% |
| Title length | 20–77 characters; median 40 |

So the corpus is **plain declarative prose, two sentences long, with no
structure at all**. Agents are already writing the right *volume*; what is
missing is organisation inside that volume.

Two consequences worth separating:

- The write path is **not** flattening anything. Verified by re-running the new
  normalization: `"Ran the suite.\n\n- 3 passed\n- 0 failed"` is stored with its
  newlines and bullets intact. The 0% above is the agents' own choice, not data
  loss.
- Nothing in the recording contract tells an agent how to shape a summary. The
  instructions say *what* to record (`_RECORDING_CONTRACT_LINES`,
  `src/agentacct/install_guide.py:282-293`) and never how to structure it beyond
  "a compact result summary".

## 2. How it renders — and why that is the problem

The text surfaces were sized for content the ledger does not contain, and the
content has no shape for the surfaces to exploit.

| Surface | What it can show | What it gets |
| --- | --- | --- |
| Canvas card | Title, lane label, result line; `lineLimit(2)` in a 200×80 pt card (`WorkTimeCanvasLayout.swift` card geometry, `WorkTimeCanvas.swift` card body) | A 40-character title, with the 27-word summary **not shown at all**; full title only on hover |
| Inspector | The full summary as one `Text` view, `lineLimit` unset (`WorkTimelineView.swift:568-570`) | Up to 1,024 characters in a single undifferentiated block |
| TUI checks | The summary **inlined into the headline line** (`src/agentacct/tui.py:2053-2055`) | A 207-character median summary on one terminal line |
| Receipt Markdown | Agent text escaped only for pipes and newlines (`receipt_markdown.py:38`) | Prose that is safe but structureless |
| All Swift surfaces | `Text(summary)` — explicitly never parsed as Markdown (`ReceiptsPane.swift:929`, `:1193`, `:1774`) | Newlines preserved in the data, flattened or ignored by the renderer |

The mismatch is therefore **not** "agents write too much". It is:

1. **The card carries a title where a reader needs a sentence.** The card is the
   only always-visible surface, and it structurally cannot show the outcome.
2. **The one surface with room for structure renders a paragraph.** The inspector
   is unbounded, but shows 27 words of prose as an undifferentiated block, so a
   three-part outcome reads as a wall.
3. **The TUI inlines prose into a line.** A 207-character summary in one row is
   the single worst text-rendering decision in the product, and it exists because
   the summary is treated as a label rather than as content.
4. **Structure is preserved but never rendered.** Refusing to parse Markdown is
   the right call — an agent's `` `--flag` `` or `**bold**` must not become
   formatting — but the result is that an agent who *does* write bullets gets a
   paragraph anyway, so there is no reason for agents to write structure and no
   reward when they do.

## 3. The copy the product invents to cover the gaps

The surfaces contain a large, inconsistent vocabulary of placeholder and apology
text, which is what a reader actually sees when data is thin.

- **At least 20 distinct phrasings** for "this field is missing", in three
  registers: neutral (`"not supplied"`, `"Recorded work"`), apologetic
  (`"command text was intentionally not captured"`, `"Source time unavailable"`),
  and identity-substitutes (`"Untitled step · <client>"`, `"Unnamed check"`,
  `"recorded check"`, `"Recorded check"`).
- **101 fallback sites** use the literal `"unknown"` as a display value across
  `api.py`, `cli.py` and the TUI, while `"unknown"` is *also* a real enum member
  in the data model (`work_events.py:31`, `:136`). A reader cannot tell a
  recorded `unknown` from a rendering gap.
- **Help and explanation copy is long and static.** 125 user-visible `Text(…)`
  strings of 12+ characters in the Swift sources; the agent-facing MCP
  instruction block is 8 lines / 1,627 characters, with two single lines over
  200 characters trying to convey a three-lever setup.
- **One string was not copy at all but an unfilled template.** The evidence
  coverage *definition* printed by the CLI, the TUI and every exported Markdown
  receipt read `X of Y checkable steps carry a passing check…` — a formula
  written for a reader of the source, shipped to a reader of the receipt. It is
  in the two published worked examples, where it reads as a renderer bug. It now
  states the rule in words (`Counts are passing checks over checkable steps,
  split by how independent each check is.`), and `test_receipt_markdown.py`
  fails on any `N of M`-shaped token in a rendered receipt.

**Inferred:** the user's complaint that explanations are "constantly visible"
follows directly from this. Every mandatory field absorbs one piece of
placeholder copy, and every rule the data does not guarantee becomes a sentence
the interface has to say.

## 4. How to regulate it

Four levers, in the order they should be pulled.

### L1. Tell agents how to shape the text (write-time)

Add a text contract to `_RECORDING_CONTRACT_LINES` — one constant, so MCP
instructions, SessionStart context and rendered `CLAUDE.md`/`AGENTS.md` all carry
it, and `docs/agentacct-workflow-instructions.md` mirrors it verbatim:

- `section_title` names the *unit of work*, not the goal of the session: a verb
  plus its object, distinguishable from sibling sections in the same session.
  (Measured: one title appears 18 times in the real ledger, so this is a real
  failure, not a hypothetical.)
- `summary` leads with the outcome in one sentence, then at most three short
  lines for what changed, what was verified, and what remains. Bullets on
  separate lines, no headings, no tables, no bold.
- `next_step` states the concrete continuation, not a restatement of the goal.

### L2. Render the structure the contract asks for (read-time)

If the contract asks for bullets, a surface has to show bullets. The safe design
is a **whitelist renderer**, not a Markdown parser: leading `-`/`*`/digit lines
become list rows, blank lines become paragraph breaks, everything else stays
literal text. That keeps the existing guarantee — agent text is never
interpreted as markup — while making structure visible. Three surfaces need it:

- the inspector (`WorkTimelineView.swift:568-570`) — the block with room for it;
- the TUI check headline (`tui.py:2053-2055`) — **stop inlining the summary into
  the headline**; give the check a first line (name/result) and a wrapped
  summary block beneath;
- the receipt Markdown — already safe, needs the same list handling so a pasted
  receipt keeps its shape.

### L3. One placeholder vocabulary, and prefer absence (copy-time)

Regulate the invented copy the way the data is regulated:

- **Absent stays absent.** A missing field is omitted, not narrated. Keep at most
  one short affordance (a muted em dash) for the cases where a reader would
  otherwise wonder whether something failed to load.
- **Never substitute an identity.** A card headline must never fall back to a raw
  `section_id`; with the summary and title both required, this path stops being
  reachable rather than being worded better.
- **Reserve "unknown" for the data model.** Display code uses absence or a
  single neutral phrase, so a recorded `unknown` is unambiguous.
- **Publish the vocabulary in one place** so the five surfaces cannot drift into
  five different apologies.

### L4. Make it enforceable (rule-time)

Proposed rules, in the RULES.md template (claim / measured why / enforcement
point / severity / false-positive budget):

| Rule | Claim | Enforced at |
| --- | --- | --- |
| **T1** | `summary` on a terminal section leads with an outcome sentence: the first line is ≥ 20 characters and contains a verb-like token, no leading bullet | MCP check — *advisory*, because a wrong refusal loses real work |
| **T2** | A summary containing a list must use one consistent bullet form; mixed markers are collapsed to `-` | normalize at write |
| **T3** | Titles within one client session must be distinct; a repeat is refused with both titles named | refused (`section_id` reuse shows this is a real pattern) |
| **T4** | No user-visible copy string exceeds 140 characters on one line; longer explanation moves behind `ContextHelp` | a source-level test over the Swift/Python string tables |
| **T5** | Display code contains no fallback literal for an identity field (no `"Unnamed check"`, `"Recorded work"`, `"Untitled step"`) | a source-level test, with the field going absent instead |

T4 and T5 are unusual: they are **tests over literals**, not runtime validators,
and that is the point — copy drift is a source problem, so the cheapest reliable
enforcement is a test that fails when a new long string or a new apology is added.

## 5. What this changes for the rules already implemented

Nothing in R1/R2/R4 conflicts with the text contract; they are the structural
half of it. Two adjustments follow from this audit:

- R2's narrative normalization preserves newlines precisely so L2 has something
  to render. It now also collapses **runs** of blank lines to one, because a run
  renders identically to a single paragraph break and only makes a card taller;
  bullets and single blank lines pass through untouched. Pinned by
  `tests/test_text_hygiene.py`.
- The refusal text for R4 now asks for the shape the reader will see
  (``"<what changed, then what was verified>"``) instead of a one-line
  placeholder, so the guidance and the rendering agree.

## 6. Text hygiene guards now in the suite

`tests/test_text_hygiene.py` — 8 tests, all passing:

| Guard | What it pins |
| --- | --- |
| Structure survives normalization | bullets, paragraphs and single blank lines are preserved; control characters are not |
| Refusals stay copyable | the R4/R5 examples are single-line literals the agent can paste, and every terminal refusal names its field |
| Identity substitutes are inventoried | the four `"Recorded work"`/`"recorded check"`/`"Recorded check"` fallbacks are a reviewed set, not a growing habit |
| One-line copy budget | outside the agent-facing prompt text the surfaces hold at most four long-form outcome explanations (126-191 characters) — the exact list to move behind `ContextHelp` |
| `unknown` ambiguity inventoried | six display sites pass a *recorded* `unknown` through; a seventh becomes a decision |

These are deliberately source-level tests. Copy drift is a source problem: a
runtime validator cannot stop a developer from adding a nineteenth apology
string, but a failing test can.

Full suite on this head: **4,348 passed, 0 failed**. When this audit was written
the branch had not yet merged `main`, and the suite read 2,834 passed with one
pre-existing failure — `test_out_of_range_timestamp_never_500s_kept_surfaces`,
which the merged `main` fixes.
