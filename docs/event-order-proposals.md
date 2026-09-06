# Event Order proposals: decisions, and what is deliberately still open

Claude transcribes a client's final details into the ten free-text Event
Order fields; Aaron approves them field by field. Built 2026-09-06 on
`feat/beo-proposals`.

**Why it exists, in Aaron's words:** manual Event Orders were the last
place he retyped client details by hand. "That's transcription work, and
it's where things go wrong: the last one put the client's decoration note
into the Dietaries field and dropped a declared nut allergy entirely."

Everything below is a decision that was actually made, with the date, so
nobody re-argues it from scratch or re-introduces a rule that was removed
on purpose.

---

## 1. Decided 2026-09-06

| Question | Decision | Where it lives |
|---|---|---|
| RSA line on an 18th with children | **Blocking only when the proposal is rewriting Special notes; a non-blocking warning otherwise.** Aaron's original rule blocked every proposal on such a booking, including a pure allergy transcription, over a pre-existing gap in a different field — and a blocked proposal is never shown to staff, so the safety rule withheld the safety data. His ruling: "A safety rule that blocks the delivery of safety data is worse than no rule." | `beo_rules._validate`, `RSA_MISSING` / `RSA_ABSENT_ON_DOCUMENT` |
| The two rules added beyond Aaron's four (`ERASES_VALUE`, `DROPS_DIETARY`) | **Keep.** Losing content was the actual failure in the incident, and none of the original four would have caught it. | `beo_rules` |
| Re-running the house rules at approval, not only at propose | **Keep.** The approval box is editable, so propose-time-only checks are decorative. | `beo_proposals._check_on_approval` |
| A playlist alongside a DJ | **Not a rule at all.** It was blocking; it is removed. The wizard's music step is a multi-select whose own comment says "a playlist before/after a DJ set is a normal event", and `wizard_generation.build_music_text` composes both lines when the client picks both. Two rules for one fact confuse whoever hits them; the wizard is the authority. | removed from `beo_rules`; regression guard in `test_no_music_combination_is_blocked` |
| Regenerate discarding approved and hand-edited values | **Fixed.** See section 2. | `app/services/document_regeneration.py` |

## 2. Regenerate no longer destroys a human value silently

This was a **live bug in the document layer**, not a limitation of
proposals. Proved on 2026-09-06 before the fix, through the real admin
route: a booking whose Event Order said `1x severe nut allergy (table 4).`
had one click of **Regenerate** applied, and the field became
`No dietary requirements declared` — the generator's default, which is the
literal opposite claim — recorded as version 2 with nothing shown to
anyone. Aaron's incident, in a new costume, with no human in the loop.

It mattered because regenerating is normal, not rare: Suzanne's Event
Order is on v3.

**What happens now.** A regenerate computes the fresh content, compares
the free-text fields against the current document, and:

- **nothing at risk → unchanged.** One click, one new version. This is the
  ordinary case and it must stay one click.
- **something at risk → HTTP 409 and a confirmation screen** naming every
  field, showing the current value and the value that would replace it,
  badged with who approved it and when where that can be traced, and
  flagged "would be emptied" where the replacement is blank or a generated
  default. Nothing is written.

Design choices worth not undoing:

- **Every box arrives ticked to keep.** The safe answer requires no
  action; the destructive one requires a deliberate untick.
- **Kept values are read from the document at write time, never from the
  form.** A value that travelled through a browser and back is not the one
  that was approved.
- **`expect` is a compare-and-set** on the exact set of losses that was
  shown. If another approval lands in between, the screen is re-shown
  rather than a stale decision applied — the same hidden-`expect` shape
  used elsewhere in the codebase.
- **Scope is the free-text fields only** (`PROTECTED_TEXT_FIELDS` — the
  ten proposable fields plus `internal_notes`, `status_text`, and the
  legacy merged music field). Derived content — the timeline, food order,
  totals, room — regenerates as before, because rebuilding it is the
  entire point of the button. Diffing everything would produce a screen
  nobody reads, and a screen nobody reads is the original silence with
  extra clicks.
- **A `[REVIEW]` prompt or the dietaries default is not a loss.** Notably
  `No dietary requirements declared` is never treated as worth protecting:
  it is the sentence that overwrote a real allergy.
- The decision is recorded as a `document_regenerated` booking event
  naming what was kept and what was replaced.

Applies to **agreements as well as Event Orders** — it is one code path
and narrowing it to Event Orders would have been arbitrary.

## 3. Open, deliberately — logged 2026-09-06, not yet scheduled

**Retention and redaction of allergy and health text.** Aaron: "Retention
and redaction can wait, but log it. Allergy text in an append-only store
with no way to redact is a problem I'd rather solve deliberately than
discover."

The exact shape of the problem:

- Approved field values land in `booking_events`, which is **append-only
  and enforced by a database trigger** — rows can be neither updated nor
  deleted. Health information about a named client is therefore currently
  **permanent**.
- An AI-written `source` summary lands in `ai_request_log`, and the
  proposal tables keep `proposed_value`, `previous_value` and
  `applied_value` for the same text.
- None of these has a retention rule, a read surface for a subject-access
  request, or any way to redact one person's data without breaking the
  audit chain the trigger exists to protect.

Nothing here is a reason to delay the feature — the alternative is the
same text retyped by hand into the same tables. It is a reason to decide
the policy before someone asks for their data to be removed. Whoever picks
this up should start with: what is the retention period, who may redact,
and how is a redaction itself audited without re-introducing the value.

Two smaller things in the same area:

- Regenerating still **re-derives all ten fields**; the fix above makes
  that visible and refusable, but the underlying model is still
  "regenerate rebuilds from the booking". Changing that means changing
  generation, which was not in scope.
- `booking_events` free text is history, not a live record — never present
  it as a current finding.
