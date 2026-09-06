"""House rules for a PROPOSED Event Order field value, enforced as
validators rather than as instructions in a prompt.

Same principle as app.services.draft_rules: these run after the proposal
arrives and before a human ever sees it, and a violation BLOCKS the whole
proposal rather than surfacing it with a warning somebody has to notice.
The cost of a blocked proposal is that Aaron retypes the fields by hand,
which is exactly what happens today; the cost of an unblocked bad one is
a wrong Event Order on the contract path.

Every rule here comes from a real failure or a real house rule:

- DIETARY_CONTAMINATION is the error that prompted the whole feature: a
  client's decoration note was transcribed into the Dietaries field, and
  a declared nut allergy was dropped. Dietaries is the one field on this
  document where being wrong is a safety matter, so anything that reads
  like decorations, a supplier or setup does not belong in it.
- CLIENT_PROSE keeps the document a run sheet. "We have organised a
  cake" is the client's voice pasted through; the floor team needs
  "Cake: client supplying".
- MUSIC_CONFLICT catches the default playlist line surviving alongside a
  DJ, which is what a half-finished transcription looks like.
- RSA_MISSING is a policy floor: an 18th with children on the booking
  must carry the RSA line on its Event Order.

Fail closed: validate() treats an unexpected error as a block, never as
a pass.
"""

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# The ten fields a proposal may touch. Deliberately NOT status, the food
# order, or any total: those are computed from the catalogue, the wizard
# and the booking, and a proposed line item with a wrong price is the
# class of error this whole feature exists to avoid.
PROPOSABLE_FIELDS: tuple[str, ...] = (
    "catering_order_and_service_style",
    "bar_structure",
    "room_layout_notes",
    "music",
    "entertainment",
    "dietaries",
    "accessibility",
    "decorations",
    "special_notes",
    "onsite_contact",
)

FIELD_LABELS = {
    "catering_order_and_service_style": "Catering order & service style",
    "bar_structure": "Bar structure",
    "room_layout_notes": "Room layout notes",
    "music": "Music",
    "entertainment": "Entertainment",
    "dietaries": "Dietaries",
    "accessibility": "Accessibility",
    "decorations": "Decorations",
    "special_notes": "Special notes",
    "onsite_contact": "Onsite contact",
}

# Generous for a run-sheet note, small enough that a pasted email body is
# obviously not a field value.
MAX_FIELD_LENGTH = 2000

# Rule codes, stable for measurement.
UNKNOWN_FIELD = "unknown_field"
EMPTY_PROPOSAL = "empty_proposal"
FIELD_TOO_LONG = "field_too_long"
DIETARY_CONTAMINATION = "dietary_contamination"
CLIENT_PROSE = "client_prose"
MUSIC_CONFLICT = "music_conflict"
RSA_MISSING = "rsa_missing"
RULES_ERROR = "rules_error"

# Decoration, supplier and setup language. Anything here in the Dietaries
# field means the transcription put the wrong paragraph in the wrong box.
# "cake" is deliberately absent: a gluten-free cake is a real dietary note.
_DIETARY_CONTAMINATION = re.compile(
    r"\bballoon|\bdecorat|\bflower|\bfloral|\barch\b|\bbackdrop|\bcentre\s?piece|\bcenter\s?piece|"
    r"\bcandle|\bsignage\b|\bbanner|\blinen|\bnapkin|\bchair\s+cover|\bfairy\s+lights|\bstyl(?:ist|ing)\b|"
    r"\bset\s?-?\s?up\b|\bsetting\s+up\b|\bbump\s?-?\s?in\b|\bpack\s?-?\s?down\b|\bdeliver(?:y|ing|ed)?\b|"
    r"\bsupplier|\bvendor|\bphotographer|\bvideographer|\bflorist|\bhire\s+company|\binstall(?:ing|ation)?\b",
    re.IGNORECASE,
)

# First-person client voice. A run-sheet note has no narrator: "DJ from
# 8pm", not "we have organised a DJ from 8pm". Possessives included --
# "our photographer" is the client's photographer, written by the client.
_CLIENT_PROSE = re.compile(
    r"(?<!\w)(?:i|i'm|i'll|i've|i'd|we|we're|we'll|we've|we'd|my|our|ours|us)(?!\w)",
    re.IGNORECASE,
)

# The default playlist line the wizard composes (see
# wizard_generation.MUSIC_TYPE_LINES["own_playlist"]) -- recognised by its
# distinctive parts rather than the whole sentence, so a lightly reworded
# copy still trips it.
_DEFAULT_PLAYLIST_LINE = re.compile(
    r"\bspotify\b.{0,80}\b(?:public|playlist\s+name|no\s+links?)\b|"
    r"\b(?:public|playlist\s+name|no\s+links?)\b.{0,80}\bspotify\b",
    re.IGNORECASE | re.DOTALL,
)
_DJ_MENTION = re.compile(r"(?<!\w)(?:dj|d\.j\.|disc\s+jockey)(?!\w)", re.IGNORECASE)

# The RSA line an 18th's Event Order must carry. The term staff and the
# agreement both use is "RSA"; anything that says it satisfies this.
_RSA_LINE = re.compile(r"(?<!\w)rsa(?!\w)|responsible\s+service\s+of\s+alcohol", re.IGNORECASE)

# What makes a booking an 18th for this rule: the form's own event type,
# or the milestone written into the event name. Mirrors
# enquiry_classification.looks_like_18th rather than re-deciding it.
_EIGHTEENTH = re.compile(r"(?<!\w)18(?:th)?(?!\w)", re.IGNORECASE)


@dataclass
class RuleViolation:
    code: str
    field: str | None
    message: str
    excerpt: str = ""


@dataclass
class RuleResult:
    violations: list[RuleViolation] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.violations)

    @property
    def codes(self) -> list[str]:
        return [v.code for v in self.violations]

    def as_note(self) -> str:
        return " ".join(v.message for v in self.violations)


def _excerpt(match: re.Match) -> str:
    return match.group(0).strip()[:120]


def looks_like_eighteenth(*, event_type: str | None, event_name: str | None) -> bool:
    if (event_type or "").strip().lower() == "18th birthday":
        return True
    return bool(_EIGHTEENTH.search(event_name or ""))


def validate(
    proposed: dict[str, str],
    *,
    effective: dict[str, str] | None = None,
    event_type: str | None = None,
    event_name: str | None = None,
    child_count: int = 0,
) -> RuleResult:
    """Check one proposal's field values.

    `proposed` is what the caller wants to set. `effective` is what the
    Event Order would read after the proposal was applied (the proposed
    value where there is one, the document's current value otherwise) --
    the RSA floor is checked against that, because the question it asks
    is about the finished document, not about this edit alone.
    """
    try:
        return _validate(
            proposed,
            effective=effective if effective is not None else dict(proposed),
            event_type=event_type,
            event_name=event_name,
            child_count=child_count,
        )
    except Exception:  # noqa: BLE001 -- fail closed
        logger.exception("Event Order proposal validation failed")
        return RuleResult(
            violations=[
                RuleViolation(RULES_ERROR, None, "The house rules could not be checked, so the proposal is withheld.")
            ]
        )


def _validate(
    proposed: dict[str, str], *, effective: dict[str, str], event_type: str | None, event_name: str | None, child_count: int
) -> RuleResult:
    result = RuleResult()

    if not proposed:
        result.violations.append(
            RuleViolation(EMPTY_PROPOSAL, None, "A proposal has to propose at least one field.")
        )
        return result

    for name, value in proposed.items():
        if name not in PROPOSABLE_FIELDS:
            result.violations.append(
                RuleViolation(
                    UNKNOWN_FIELD, name,
                    f"'{name}' is not a proposable field. Status, the food order and every total are computed.",
                )
            )
            continue
        if len(value or "") > MAX_FIELD_LENGTH:
            result.violations.append(
                RuleViolation(
                    FIELD_TOO_LONG, name,
                    f"{FIELD_LABELS[name]} is {len(value)} characters; the limit is {MAX_FIELD_LENGTH}. "
                    "This reads like a pasted email rather than a run-sheet note.",
                )
            )

    dietaries = proposed.get("dietaries") or ""
    match = _DIETARY_CONTAMINATION.search(dietaries)
    if match:
        result.violations.append(
            RuleViolation(
                DIETARY_CONTAMINATION, "dietaries",
                "Dietaries carries decoration, supplier or setup language. That belongs in Decorations or "
                "Special notes, and a dietary requirement lost behind it is a safety matter.",
                _excerpt(match),
            )
        )

    for name, value in proposed.items():
        if name not in PROPOSABLE_FIELDS:
            continue
        match = _CLIENT_PROSE.search(value or "")
        if match:
            result.violations.append(
                RuleViolation(
                    CLIENT_PROSE, name,
                    f"{FIELD_LABELS[name]} is written in the client's voice. The Event Order is a run sheet: "
                    "state the fact, not the sentence they wrote.",
                    _excerpt(match),
                )
            )

    music = proposed.get("music") or ""
    if _DEFAULT_PLAYLIST_LINE.search(music) and _DJ_MENTION.search(music):
        result.violations.append(
            RuleViolation(
                MUSIC_CONFLICT, "music",
                "Music carries both the default Spotify playlist line and a DJ. One of them is left over from "
                "the template; confirm which the client actually has.",
            )
        )

    if child_count > 0 and looks_like_eighteenth(event_type=event_type, event_name=event_name):
        if not _RSA_LINE.search(effective.get("special_notes") or ""):
            result.violations.append(
                RuleViolation(
                    RSA_MISSING, "special_notes",
                    "This is an 18th with children on the booking, so Special notes has to carry the RSA line. "
                    "Propose a Special notes value that states it.",
                )
            )

    return result
