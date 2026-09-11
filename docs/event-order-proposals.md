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
| A proposal arriving while a field is mid-approval (ultrareview) | **Fixed by compare-and-set, not by a Python read.** `_supersede_older` read field state, then queued an unconditional UPDATE; the advisory lock serialises proposes with each other only, so the UPDATE could block behind an approval's row lock and overwrite the just-committed APPROVED with SUPERSEDED — the document kept the value while the record said it was never applied, invisible to every count of approvals. Both the field rows and the parent proposal are now `UPDATE … WHERE state = 'pending'`, which Postgres re-evaluates after the lock is granted. The review's own two-session test passes against it. | `beo_proposals._supersede_older` |
| Approving Music alone on an older Event Order (ultrareview) | **Refused, at propose and at approval.** Pre-wizard Event Orders carry ONE merged `music_entertainment` value, printed under Music until a split `music` exists; approving Music cleared it, so "DJ 8pm + Magician from 9pm" became "DJ 8pm" and the magician vanished from the run sheet. `LEGACY_MUSIC_SPLIT` blocks unless Entertainment is written too or already holds text, or Music carries the whole value. It ignores the generator's own `[REVIEW]` prompt and is dormant once `music` is split — the first cut got that wrong and refused every proposal on a fresh Event Order. | `beo_rules.LEGACY_MUSIC_SPLIT`, `beo_proposals._printed_legacy_music` |
| Regenerate screen calling an approval a hand-edit (ultrareview, nit) | **Fixed at the event-type depth.** Approvals wrote the same `document_edited` event a hand-edit does, so the screen said "hand-edited by X" beside a badge saying "approved by X". `update_content_fields` now takes the event type; an approval records `beo_proposal_applied`. | `documents.update_content_fields`, `beo_proposals._apply` |

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
- **Scope is `PROTECTED_FIELDS`** — the ten proposable fields, plus
  `internal_notes`, `status_text` and the legacy merged music field, plus
  the agreement's `terms_sections`. Derived content — the timeline, food
  order, totals, room — regenerates as before, because rebuilding it is
  the entire point of the button. Diffing everything would produce a
  screen nobody reads, and a screen nobody reads is the original silence
  with extra clicks.
- **The agreement's terms are a list of {heading, body}, not a string**, so
  they are rendered to text for comparison and display. `terms_text` is
  rebuilt from them, so it is a *companion*: kept or replaced with the
  sections, never separately, or the contract would state two different
  sets of terms.
- **A row lock is held across the read and the write.** Without it an
  approval landing in between was silently reverted while the audit line
  still said the value was kept. The lock alone was not enough: because a
  regenerate creates a NEW version, an approval that had been waiting on
  the lock would then apply to the row it had locked — now superseded —
  and report success. `beo_proposals._locked_draft` therefore re-checks
  `is_current` after acquiring the lock and refuses, telling the approver
  to reload. Both halves were proved live with two real sessions.
- **A pending proposal stops a regenerate too, and says why.** A
  proposal is reviewed against one version; regenerating makes a new one,
  so the proposal survives but must be approved again, and approving it in
  the meantime now fails with the message above. The screen names the
  pending fields and links to the review BEFORE anything is written --
  including on the path with nothing else to lose, which shows no screen
  at all otherwise and so was the one place pending work would have been
  invalidated in silence. Aaron: "Tell me before, not after."
- **A generated placeholder is not a loss, matched EXACTLY.** Notably
  `No dietary requirements declared` is never treated as worth protecting:
  it is the sentence that overwrote a real allergy. The match is exact and
  never a substring — staff reuse the `[REVIEW]` convention in their own
  notes, and a substring test silently regenerated over "Client bringing
  cake. [REVIEW] confirm nut-free with kitchen".
- The decision is recorded as a `document_regenerated` booking event
  naming what was kept and what was replaced.

Applies to **agreements as well as Event Orders**. The first cut claimed
that and did not deliver it: every protected name was an Event Order
field, so a hand-edited contract clause was still discarded silently. The
agreement is the contract, so that was the more serious half.

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


## 4. Decided 2026-09-11 — moving the boundary

Aaron, after the first live use on HAM-20260926-FM49Q: "it made more work,
not less … Ten clicks plus typing the food is more effort than pasting the
text and typing the food. The problem is where the boundary sits. 'The AI
can't write money' is correct and stays. But the food order is where the
actual work is." The principle is unchanged: nothing reaches a client
document without a staff member, and the AI never writes a price.

| Question | Decision | Where it lives |
|---|---|---|
| A proposal on a booking with no Event Order | **Creates the first draft** when the booking is tentative, confirmed or completed (an enquiry's Event Order stays a staff decision; the proposal is stored and the API says so). The rules are judged against the content the draft WOULD hold — wizard answers if submitted, else the booking's facts with `[REVIEW]` prompts — BEFORE anything is written, so a blocked proposal creates nothing and what was checked is what gets created. The draft, its `beo_draft_by_proposal` trail row and the proposal are written in ONE transaction under the per-booking advisory lock; "first version" is serialised on the booking row, which always exists (with no Event Order there is no document row to lock), and the staff Generate paths take the same lock — a lost race is a 409 "propose again", never a 500. A current Event Order that has gone out is never superseded: the proposal is judged against that version's values (what a Revise copies forward) and waits for the Revise, which the API says. An AI-made draft does not reset the pipeline's `days_at_stage` clock and does not clear the `NOTES_BEFORE_BEO` reconciliation finding — nobody has read the notes yet. The trail carries `document_created` (actor `ai:claude`) and `beo_draft_by_proposal`; the API answers `event_order: {version, status, created}`. | `beo_proposals.propose`, `_create_draft_for_proposal`, `fresh_beo_content` (one builder shared with the staff Generate click), `documents.lock_booking_row`, `create_new_version(commit=False)` |
| Approve all, with edit | Was already built (`approve_all`, the `value_*` boxes, the footer button) but sat below ten per-field Approve buttons, so it read as the afterthought. **It is now the primary action at the top of the panel**; per-field buttons read "Approve only X" and are secondary. Nothing else changed: edits in the boxes are recorded as `beo_proposal_edited`, rules run again at approval. | `document_edit_beo.html` |
| The food order as catalogue items and quantities | **Decided, built in the following commits** (its own section when it lands): the AI proposes `[{menu item, quantity}]`, never a price, never a custom line; the price comes from the catalogue exactly as for the wizard; staff see the lines and the computed total, approve or edit quantities; the Event Order line items and the draft final invoice are built from the catalogue on approval. | to follow |
