"""What a regenerate is about to destroy, named field by field.

Regenerating a document builds fresh content from the booking and replaces
the current version with it. For everything the booking computes -- the
timeline, the food order, totals, the room -- that is exactly right and is
the whole reason the button exists.

For the fields a person writes in their own words it is not. Those values
are not derivable from anything: an approved allergy note, a hand-edited
contract clause. Regenerating discarded them silently, and silence is the
worst property that failure could have. Proved on 2026-09-06, twice:

  - generate_beo_content defaults Dietaries to "No dietary requirements
    declared", so one click of Regenerate replaced a declared nut allergy
    with that sentence and made it version 2 -- Aaron's original incident,
    in a new costume, with no human in the loop at all;
  - regenerating an AGREEMENT discarded a hand-edited special condition
    ("Client may bring their own celebrant. Agreed by Aaron.") the same
    way. That one is the contract.

So a regenerate that would destroy a human value now stops and says
exactly what it is about to discard, and the human decides per field.
Nothing here decides for them; it only refuses to decide silently.

Scope, deliberately: the fields below and no others. Diffing the derived
structures too would produce a screen nobody reads, and a screen nobody
reads is the silence this module exists to end.
"""

import datetime as dt
import hashlib
import logging
from decimal import Decimal
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BookingEvent, Document
from app.models.beo_proposal import FIELD_APPROVED, BeoProposal, BeoProposalField
from app.services import beo_rules, content_authorship
from app.services.document_generation import NO_DIETARIES, REVIEW

logger = logging.getLogger(__name__)


def _render_text(value: object) -> str:
    """One spelling for comparison. A CRLF/LF difference is not an edit --
    the same lesson as the approval box (2026-09-06 review)."""
    if not isinstance(value, str):
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _render_amount(value: object) -> str:
    """A quantity or a price as one comparable figure.

    The edit form writes str(Decimal(...)) and the wizard writes str(price),
    so "250" and "250.00" are the same money and a formatting difference is
    not a loss. f"{d:f}" rather than str(d): str(Decimal("250.00").normalize())
    is "2.5E+2", which is not a thing to put on a confirmation screen.
    """
    try:
        return f"{Decimal(str(value)).normalize():f}"
    except (ArithmeticError, TypeError, ValueError):
        return _render_text(value)


def _render_food_order(value: object) -> str:
    """The food order as the money it commits to, one line per item.

    A food order is a dict, and _render_text answers "" for anything that is
    not a str -- so food_order in the table WITHOUT this compares "" against
    "" for ever, skips every time, and reads as protection on every screen
    while protecting nothing. Proved by running it before this existed.

    Not the stored dict either. `note` holds the generator's own "[REVIEW] no
    food order captured yet" and `category` only picks which heading a line
    prints under, so neither is a value anybody would miss. Sorted, because a
    reorder costs nobody anything and a warning about one is a warning staff
    learn to click through -- and this screen only works while it is worth
    reading.
    """
    if not isinstance(value, dict):
        return ""
    items = value.get("line_items")
    if not isinstance(items, list):
        return ""
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # "item" is the pre-rename key document.html still reads.
        description = _render_text(item.get("description") or item.get("item"))
        lines.append(
            f"{_render_amount(item.get('quantity'))} x {description}"
            f" @ {_render_amount(item.get('unit_price'))}"
        )
    return "\n".join(sorted(lines))


def _render_sections(value: object) -> str:
    """An agreement's terms are a list of {heading, body}, not a string.
    Rendered to text so the same comparison and the same screen work for
    them -- without this they compared as "" and the contract's own clauses
    were the one thing the guard could not see (2026-09-06 review)."""
    if not isinstance(value, list):
        return ""
    blocks = []
    for section in value:
        if not isinstance(section, dict):
            continue
        heading = _render_text(section.get("heading"))
        body = _render_text(section.get("body"))
        if heading or body:
            blocks.append(f"{heading}\n{body}".strip())
    return "\n\n".join(blocks)


@dataclass(frozen=True)
class ProtectedField:
    name: str
    label: str
    render: Callable[[object], str] = _render_text
    # Fields derived from this one, which must travel with it. An
    # agreement's terms_text is rebuilt from terms_sections, so keeping the
    # sections while letting the text regenerate would leave the document
    # stating two different sets of terms.
    companions: tuple[str, ...] = ()


_TEXT_FIELD_LABELS = {
    **beo_rules.FIELD_LABELS,
    "music_entertainment": "Music & entertainment",
    "internal_notes": "Internal notes (staff/kitchen)",
    "status_text": "Status text",
}

# The Event Order's ten proposable fields, the two free-text fields that
# predate proposals, the legacy merged music field -- and the agreement's
# terms, which are the contract itself.
PROTECTED_FIELDS: tuple[ProtectedField, ...] = tuple(
    ProtectedField(name, _TEXT_FIELD_LABELS.get(name, name.replace("_", " ").capitalize()))
    for name in beo_rules.PROPOSABLE_FIELDS + ("music_entertainment", "internal_notes", "status_text")
) + (
    ProtectedField("terms_sections", "Agreement terms", render=_render_sections, companions=("terms_text",)),
    # The one protected field that is money rather than words. total_food_spend
    # is DERIVED from these lines (document_generation.build_total_food_spend),
    # so it travels with them: keeping the lines alone produced a live document
    # whose own items summed to $1,220 under a heading reading $1,250.
    ProtectedField(
        "food_order", "Food order", render=_render_food_order, companions=("total_food_spend",)
    ),
)

PROTECTED_FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in PROTECTED_FIELDS)
_BY_NAME = {f.name: f for f in PROTECTED_FIELDS}

# Values the GENERATOR produces when nothing was captured. Matched exactly,
# never as a substring: staff reuse the [REVIEW] convention in their own
# notes, and "Client bringing cake. [REVIEW] confirm nut-free with kitchen"
# is a human sentence carrying an allergy follow-up, not a placeholder
# (2026-09-06 review -- a substring test silently regenerated over it).
# test_every_generated_placeholder_is_recognised keeps this in step with
# the generator.
GENERATED_PLACEHOLDERS = frozenset({
    f"{REVIEW} add catering order and service style",
    f"{REVIEW} add bar structure",
    f"{REVIEW} add room layout notes",
    f"{REVIEW} add music/entertainment detail",
    NO_DIETARIES,
})


def read_music_as_split(content: dict) -> dict:
    """Content with a legacy merged music value read as `music`.

    The Event Order prints one Music section, `music or
    music_entertainment` (document.html), so the two keys are two
    spellings of one value and "would this be lost" is a question about the
    section, not about a key. Asked per key it came out backwards: a
    document whose merged field held a person's "Live band, 8pm-11pm" and a
    rebuild carrying a split `music` produced a loss row saying Music &
    entertainment would be EMPTIED, and keeping it wrote the merged value
    back UNDERNEATH the new `music`, where the template never shows it.
    The confirmation screen, the regenerate audit line and the wizard's
    outstanding items all said the words were kept; the run sheet printed
    the other value. Half a booking's entertainment, lost with three
    separate assurances that it had not been.

    THE RECORD TRAVELS WITH THE VALUE. If the authorship record named
    `music_entertainment`, the promoted content records `music` and
    forgets the old name -- a RENAME, never a new claim. Silence stays
    silence: content with no record still has none afterwards, and a
    document nobody has stamped is not stamped here.

    Without it the words arrive somewhere the record does not describe,
    and the next writer drops the name for a key that is now empty. Proved
    through the client wizard: a legacy Event Order whose merged field was
    recorded as a person's came out with the words in `music` and
    `_authored` EMPTY -- has_record True, authored() empty, which is this
    module's positive encoding for "nothing here is a person's". The same
    document's outstanding items said "Music kept from the previous Event
    Order". The record asserted the opposite of the note beside it.

    The staff edit form already reaches the same answer from the other
    direction: it prefills the Music box from `music or
    music_entertainment`, saves the value into `music`, and records
    `music`. This is the regenerate side agreeing with it.

    No companion field is needed to go with the value. The template
    prefers `music`, a kept `music` is non-empty by definition, and
    generated content only ever puts None or the [REVIEW] prompt in the
    merged field -- so whatever is left there afterwards cannot print and
    cannot be mistaken for somebody's words on the next pass.

    Read here rather than fixed in the table because the shapes are not
    equivalent: only the legacy spelling is rewritten, and only when it is
    what prints. A merged value sitting behind a split `music` is dead
    weight, and the generator's own [REVIEW] prompt is not a person's
    words -- the same two exclusions beo_proposals._printed_legacy_music
    makes for the same reason.
    """
    legacy = content.get("music_entertainment")
    if content.get("music") or not isinstance(legacy, str):
        return content
    if not legacy.strip() or legacy.lstrip().startswith(REVIEW):
        return content
    promoted = {**content, "music": legacy, "music_entertainment": None}
    if "music_entertainment" in content_authorship.authored(content):
        promoted = content_authorship.forget(
            content_authorship.record(promoted, ["music"]), ["music_entertainment"]
        )
    return promoted


def _is_disposable(rendered: str) -> bool:
    """True when the current value holds nothing a human would miss."""
    return not rendered or rendered in GENERATED_PLACEHOLDERS


def _was_cleared(stored: object, rendered: str) -> bool:
    """Whether this field is empty because somebody emptied it.

    Read from the STORED value, not from `rendered` being blank. _render_text
    returns "" for anything that is not a str, so a field holding a dict, a
    list, a number or a bool renders exactly like a field a person cleared --
    and JSONB holds whatever was written to it, which is why every read path
    in this codebase tolerates the wrong type rather than trusting it.

    Deciding from the rendered string let the screen tell a staff member
    that a colleague had deliberately emptied a field nobody had touched
    (review of c7ed188). A claim about what a person did must rest on the
    value itself, never on the shape of its rendering.
    """
    if rendered:
        return False
    return stored is None or (isinstance(stored, str) and not stored.strip())


@dataclass(frozen=True)
class ContentLoss:
    """One field whose human-written value a regenerate would destroy."""

    field: str
    label: str
    current: str
    incoming: str
    # Set when this exact value can be traced to an approval, so the
    # screen can say who approved it and when rather than "something".
    approved_note: str | None = None
    # This field is empty because somebody cleared it, and the record says
    # so. Carried explicitly rather than inferred from `current` being
    # blank: the screen states it in words, and a claim about what a person
    # did must come from the place that knows, not from a template reading
    # a coincidence. Without it the row rendered as an unlabelled empty box
    # and the one thing it exists to say went unsaid (review of e7029f2).
    cleared_by_a_person: bool = False

    @property
    def empties_the_field(self) -> bool:
        """The worst shape: real words replaced by nothing or by a
        generated default that asserts the opposite."""
        return _is_disposable(self.incoming)


def _approved_values(db: Session, booking_id) -> dict[str, list[BeoProposalField]]:
    rows = db.scalars(
        select(BeoProposalField)
        .join(BeoProposal, BeoProposalField.proposal_id == BeoProposal.id)
        .where(BeoProposal.booking_id == booking_id, BeoProposalField.state == FIELD_APPROVED)
        .order_by(BeoProposalField.decided_at)
    ).all()
    by_field: dict[str, list[BeoProposalField]] = {}
    for row in rows:
        by_field.setdefault(row.field, []).append(row)
    return by_field


def _approval_note(
    rows: list[BeoProposalField], current: str, *, hand_edited_at: dt.datetime | None = None
) -> str | None:
    """"This exact text was approved" -- not "this field was approved once".

    The badge is the screen's only statement about WHO put a value there,
    and it is made from string equality between the value on the document
    and something approved earlier. That is enough when nothing has
    happened since. It is not enough once somebody has hand-edited the
    draft: approve a sentence, edit it away, edit it back, and the screen
    credits the approver for text the editor typed.

    The authorship record cannot settle it, because an approval records
    authorship too -- both paths run through the same writer. What can is
    the timing: hand-edits are logged per document VERSION, so if one
    post-dates the approval this cannot say whether it touched THIS field,
    and it says nothing rather than guessing. Fewer badges, no false ones
    (review of 62df22f).
    """
    if not current:
        # An empty current value matches any approval whose applied_value
        # was empty or NULL, and the screen would then badge a blank field
        # "approved by Sally on 3 Sep" for something Sally never approved.
        # Unreachable until losses() began reporting cleared fields; a false
        # statement about who authorised a value is the one claim this whole
        # branch exists to stop the system making (review of 55f39ac).
        return None
    for row in reversed(rows):
        if _render_text(row.applied_value) == current:
            if hand_edited_at is not None and (row.decided_at is None or hand_edited_at > row.decided_at):
                # Somebody typed into this draft after the approval. Which
                # field they touched is not recorded, so the honest answer
                # is silence: an earlier matching approval would be older
                # still, so there is nothing further back worth checking.
                return None
            # No %-d: it is a glibc extension and raises on Windows, where
            # the tests run.
            when = row.decided_at.strftime("%d %b %Y").lstrip("0") if row.decided_at else "an earlier date"
            who = row.decided_by or "staff"
            return f"approved by {who} on {when}"
    return None


def was_hand_edited(db: Session, document: Document) -> BookingEvent | None:
    """Whether THIS version carries a hand-edit.

    The event is recorded per document version, so this answers "was this
    draft hand-edited" and the screen says exactly that rather than
    guessing. Since 2026-09-08 the event also carries the names of the
    fields the save changed, in old_value -- this function does not read
    them, but a screen that wanted to be more specific now could.
    """
    return db.scalars(
        select(BookingEvent)
        .where(
            BookingEvent.booking_id == document.booking_id,
            BookingEvent.event_type == "document_edited",
            BookingEvent.field_name == f"{document.type.value}_version",
            BookingEvent.new_value == str(document.version),
        )
        .order_by(BookingEvent.created_at.desc())
        .limit(1)
    ).first()


def _last_hand_edit_at(db: Session, document: Document) -> dt.datetime | None:
    """When a document of this type on this booking was last hand-edited,
    in ANY version.

    Deliberately not was_hand_edited(), which is scoped to one version
    because the screen uses it to say "this draft was hand-edited". The
    approval badge asks a different question -- is this TEXT still the
    approved text -- and text outlives versions: a regenerate that keeps a
    value carries it into a new version that has no edit events of its own.
    Scoped per version, the badge came back after that regenerate and
    credited the approver for words somebody had typed on the version
    before (review of d24aba5, proved live).

    The cost is real and is the same trade already made for an approval
    with no timestamp: after any hand-edit, approvals on this booking's
    documents of this type go unbadged until a newer approval. Fewer true
    badges, no false ones.
    """
    return db.scalars(
        select(BookingEvent.created_at)
        .where(
            BookingEvent.booking_id == document.booking_id,
            BookingEvent.event_type == "document_edited",
            BookingEvent.field_name == f"{document.type.value}_version",
        )
        .order_by(BookingEvent.created_at.desc())
        .limit(1)
    ).first()


def losses(db: Session, document: Document | None, fresh: dict) -> list[ContentLoss]:
    """The human-written values `fresh` would destroy, in field order.

    A field is reported when its value would change, unless:

      - the current value is one of the generator's own placeholders. Its
        sentence, nobody else's, so replacing it loses nothing. This is a
        flat skip and deliberately not a question about authorship: even
        where the record names the field, a placeholder is still not a
        person's words;
      - the current value is EMPTY and either no one has recorded writing
        it, or what would replace it is a placeholder or blank too --
        nothing to nothing is not a loss anybody needs to decide about.

    That second clause is the change. An empty field used to be skipped
    unconditionally, so a value a person had deliberately cleared was
    refilled by the next regenerate without a word -- proved live before
    this commit: `music` cleared and recorded, the booking still naming a
    DJ, and losses() returned nothing at all. Clearing a field is a
    decision, and the record is what lets this tell it apart from a field
    nobody has ever filled in.

    WHAT THIS DOES NOT DO, and why. It does not treat the record's SILENCE
    as evidence. A field the record fails to name is still reported exactly
    as before, so consulting the record can only ADD a warning, never
    remove one.

    The temptation is obvious -- the record would strip out every warning
    about a value the generator itself produced, which is most of the noise
    on this screen. It is wrong today because a record can be partial. A
    draft written before the record existed carries no record at all; the
    first hand-edit after it shipped creates one naming that single field,
    and every older human value on the document goes unnamed. Trusting
    silence would leave exactly those values unprotected -- an allergy note
    typed last month, invisible to the guard, destroyed by the next
    regenerate. That is Aaron's original incident, reached through the very
    mechanism built to prevent it, and it is how the first attempt at this
    redesign failed (reverted, 2026-09-07).

    Making silence trustworthy needs a way to know a record is COMPLETE --
    that it has been present since the content was created, so anything it
    omits really is the generator's. That is a separate change and is not
    made here.

    An empty result means a regenerate is safe to run straight through,
    which is the ordinary case.

    This is a READ. A caller that intends to write must hold the document's
    row lock across both, or another approval can land in between and be
    reverted (proved live, 2026-09-06) -- see
    documents.lock_current_for_update.
    """
    if document is None:
        return []
    current_content = read_music_as_split(document.content or {})
    approved = _approved_values(db, document.booking_id)
    # One query, not one per field. A hand-edit after an approval makes the
    # approval badge unprovable for every field, because hand-edits are
    # logged per document version rather than per field -- and across
    # versions, because the text a badge describes outlives them.
    hand_edited_at = _last_hand_edit_at(db, document)

    # Who wrote what, where anybody has said so. Read, never inferred: a
    # field the record does not name is NOT therefore the generator's, for
    # the reason set out below.
    authored = content_authorship.authored(current_content)

    found: list[ContentLoss] = []
    for spec in PROTECTED_FIELDS:
        current = spec.render(current_content.get(spec.name))
        incoming = spec.render(fresh.get(spec.name))
        if current == incoming:
            continue
        if current in GENERATED_PLACEHOLDERS:
            # The generator's own sentence. Nobody wrote it, so replacing
            # it loses nothing -- true whatever the record says, which is
            # why this stays a skip rather than becoming a question about
            # authorship.
            continue
        if not current and (spec.name not in authored or _is_disposable(incoming)):
            # Empty and unclaimed: a field nobody has filled in yet, and
            # filling it in is the regenerate doing its job.
            #
            # Empty, claimed, but the incoming value is a placeholder or
            # blank too: nothing to nothing. Reporting it put half the new
            # warnings on this screen in front of a person deciding about
            # a change from no words to no words -- wearing the "would be
            # emptied" badge, which is the loudest thing on the page. A
            # screen nobody reads is the silence this module exists to
            # end, so it stays quiet here (review of 55f39ac).
            continue
        found.append(
            ContentLoss(
                field=spec.name,
                label=spec.label,
                current=current,
                incoming=incoming,
                approved_note=_approval_note(
                    approved.get(spec.name, []), current, hand_edited_at=hand_edited_at
                ),
                cleared_by_a_person=_was_cleared(current_content.get(spec.name), current),
            )
        )
    return found


# Protected fields whose emptiness is not a lie on the page, and which
# therefore do not belong in the staff copy's "not filled in" list.
#
#   music_entertainment -- the older spelling of the Music section. After
#     read_music_as_split, `music` answers for that section under either
#     shape; naming both would report one gap twice.
#   internal_notes -- staff scratch that never reaches the client document
#     at all, so it cannot be mistaken for content on it.
#   status_text -- optional override for a status the document computes
#     anyway, so empty prints the real status rather than a plausible lie.
_NOT_A_GAP_ON_THE_PAGE = frozenset({"music_entertainment", "internal_notes", "status_text"})


def unfilled_fields(content: object) -> list[str]:
    """The fields nobody has filled in, by label, for the staff copy.

    A document prints something plausible for an empty field and there is
    no way to tell it from a field whose content was lost. Room layout
    prints the client placeholder "To be confirmed - contact the venue".
    Special notes prints the generated guest counts. Decorations prints no
    section at all. Onsite contact printed the CLIENT'S OWN NAME. On
    HAM-20260911-AKPSO on 2026-09-08 a whole page of typed content never
    reached the server and the finished document was indistinguishable
    from one nobody had touched.

    VALUE-BASED, not record-based, and that is the load-bearing decision.
    `_authored` is not written at generation time at all -- a fresh
    document has no record -- and its SILENCE is never evidence: almost
    every document on production predates the record, so asking it would
    answer "nobody wrote this" about every field of every one of them.
    That is precisely the reasoning that got the first provenance design
    reverted (see losses() above). The value knows: a [REVIEW] marker, an
    empty field, or the generator's own sentence.

    Only keys the content actually HAS, so an agreement is not reported as
    missing ten Event Order fields and vice versa. The music pair is read
    the way it prints (read_music_as_split), so a legacy Event Order whose
    detail sits in the merged field is not called empty, and the section is
    named once rather than under both of its spellings.

    And only fields that can MISLEAD somebody reading the page. A warning
    that is always there is furniture -- staff stop reading it, which is
    the failure this exists to prevent, not a smaller version of it.
    """
    values = content if isinstance(content, dict) else {}
    values = read_music_as_split(values)
    missing = []
    for spec in PROTECTED_FIELDS:
        if spec.name not in values or spec.name in _NOT_A_GAP_ON_THE_PAGE:
            continue
        if _is_disposable(spec.render(values.get(spec.name))):
            missing.append(spec.label)
    return missing


def fingerprint(found: list[ContentLoss], pending: list[dict]) -> str:
    """Identifies the exact question a human was shown.

    The confirmation screen carries this back, and the write refuses if it
    no longer matches -- a compare-and-set, the same shape as every other
    toggle in this codebase. It guards against answering a question about
    values that have since changed; it is NOT a substitute for the row
    lock, because on its own it leaves a window between check and write.

    The question is BOTH halves of that screen. It covers the losses,
    which is what a person decides about, and the pending proposals, which
    is what the regenerate will invalidate -- and which are the whole
    reason the screen appears at all when nothing is at risk. A proposal
    approved, rejected or superseded in between changes what going ahead
    costs, so it has to change the token too (Aaron: "If a regenerate
    silently invalidates pending work, I will hit exactly that error
    without knowing why. Tell me before, not after.")

    `pending` is not optional, deliberately. A default would let one of
    the two call sites -- the render that mints the token and the write
    that checks it -- be updated without the other, and a token that
    disagrees with itself refuses every regenerate forever.

    Each pending row is folded in by its field row id, which is what
    changes when a proposal is replaced by a newer one, and by its field
    name, which is stable per row (the relationship is ordered by it), so
    the digest does not depend on query order.
    """
    digest = hashlib.sha256()
    for loss in found:
        digest.update(f"{loss.field}\x00{loss.current}\x00{loss.incoming}\x00".encode("utf-8"))
    for row in pending:
        digest.update(f"pending\x00{row.get('id')}\x00{row.get('field')}\x00".encode("utf-8"))
    return digest.hexdigest()[:32]


def apply_choices(fresh: dict, document: Document, keep_fields: set[str]) -> dict:
    """Fresh content with the kept fields taken from the CURRENT document.

    Values come from the document as it stands at write time, never from
    the form: a value that travelled through a browser and back is a value
    that could have gone stale or been tampered with, and this is the
    contract path.
    """
    content = dict(fresh)
    # The same reading the question was asked about, or a kept answer
    # writes back something the person was never shown.
    current_content = read_music_as_split(document.content or {})
    for name in keep_fields:
        spec = _BY_NAME.get(name)
        if spec is None:
            continue
        content[spec.name] = current_content.get(spec.name)
        for companion in spec.companions:
            # Derived from the kept value; letting it regenerate would
            # leave the document asserting two different things.
            content[companion] = current_content.get(companion)
    return content


def summarise(found: list[ContentLoss], keep_fields: set[str]) -> str:
    """One audit line: what a human chose to keep and what they let go."""
    kept = sorted(loss.label for loss in found if loss.field in keep_fields)
    replaced = sorted(loss.label for loss in found if loss.field not in keep_fields)
    parts = []
    if kept:
        parts.append("kept " + ", ".join(kept))
    if replaced:
        parts.append("replaced " + ", ".join(replaced))
    return "; ".join(parts) or "no human values affected"
