"""House rules for a PROPOSED Event Order field value, enforced as
validators rather than as instructions in a prompt.

Same principle as app.services.draft_rules: these run before a human sees
a proposal AND again on the value actually being written at approval, and
a violation BLOCKS rather than warning somebody who has to notice. The
cost of a block is that Aaron retypes the field, which is what happens
today; the cost of a miss is a wrong Event Order on the contract path.

Running at approval as well as at proposal is deliberate (2026-09-06
review): the approval screen lets Aaron edit the text before approving,
so a rule that only ran at proposal time could be walked straight past by
pasting into the box.

Every rule comes from a real failure or a real house rule:

- DIETARY_CONTAMINATION is the error that prompted the feature: a
  client's decoration note was transcribed into the Dietaries field, and
  a declared nut allergy went with it. Dietaries is the one field where
  being wrong is a safety matter, so anything reading like decorations,
  a supplier or setup does not belong in it.
- ERASES_VALUE and DROPS_DIETARY are the other half of that same
  incident. A transcription that simply loses something is not caught by
  any rule about what the text SAYS, so these two compare the proposal
  with the value it would replace: a field is never silently emptied,
  and a declared dietary requirement is never quietly dropped.
- CLIENT_PROSE keeps the document a run sheet. "We have organised a
  cake" is the client's voice pasted through; the floor team needs
  "Cake: client supplying".
- RSA_MISSING is a policy floor for an 18th, OR for any booking with
  under-18s on it. Either alone is enough; a guest count must never be
  able to remove a compliance line (Aaron, 2026-09-09).
- LEGACY_MUSIC_SPLIT: older Event Orders carry ONE merged
  music_entertainment value, and the template prints it under Music until
  a split `music` exists. Approving Music alone clears the merged field,
  so whatever part of it was entertainment vanishes from the printed run
  sheet -- "DJ 8pm + Magician from 9pm" becomes "DJ 8pm" and the magician
  is gone (ultrareview, 2026-09-06). Blocked unless Entertainment is
  written too, already holds text, or Music carries the whole value.

There is deliberately NO rule about a playlist alongside a DJ. One existed
and was wrong: the wizard's music step is a multi-select whose own comment
says "a playlist before/after a DJ set is a normal event", and
wizard_generation.build_music_text composes BOTH lines when the client
picks both. So the pattern this rule called a half-finished transcription
is a correct Event Order for a real and common booking, and the rule
refused to transcribe anything into Music for those clients. Two rules for
one fact confuse whoever hits them (Aaron, 2026-09-06). The wizard is the
authority on what a valid music answer looks like.

Fail closed: validate() treats an unexpected error as a block.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field

from app.services.enquiry_classification import looks_like_18th

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
# obviously not a field value. Enforced on the AI's payload and again on
# what a human types into the approval box.
MAX_FIELD_LENGTH = 2000

# Rule codes, stable for measurement.
UNKNOWN_FIELD = "unknown_field"
EMPTY_PROPOSAL = "empty_proposal"
FIELD_TOO_LONG = "field_too_long"
ERASES_VALUE = "erases_value"
DROPS_DIETARY = "drops_dietary"
DIETARY_CONTAMINATION = "dietary_contamination"
CLIENT_PROSE = "client_prose"
LEGACY_MUSIC_SPLIT = "legacy_music_split"
RSA_MISSING = "rsa_missing"
RULES_ERROR = "rules_error"

# Warning codes: surfaced on the review screen, never blocking.
RSA_ABSENT_ON_DOCUMENT = "rsa_absent_on_document"

# Zero-width and formatting characters a copy-paste drags in, which would
# otherwise split a word in half and walk it past every pattern below.
_INVISIBLE_CHARS = "​‌‍⁠﻿­"
_INVISIBLE = dict.fromkeys(map(ord, _INVISIBLE_CHARS), None)
# The same characters replaced by a space rather than deleted. Deleting is
# right when an invisible has been pasted INTO a word ("n<ZWSP>ut" -> "nut");
# it is wrong when one sits BETWEEN two words ("nut<ZWSP>allergy" ->
# "nutallergy"), which the old substring test did not care about but a
# word-boundary match does. declared_dietaries reads both spellings and
# takes the union, so neither paste can hide a declaration.
_INVISIBLE_AS_SPACE = dict.fromkeys(map(ord, _INVISIBLE_CHARS), " ")


def _fold(text: str | None, invisible: dict) -> str:
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", str(text)).translate(invisible)
    return folded.replace("’", "'").replace("‘", "'")


def normalise(text: str | None) -> str:
    """One spelling of a value for matching: compatibility-normalised, with
    invisible characters and curly quotes folded away. Never used for
    storage -- only for deciding whether a pattern matches."""
    return _fold(text, _INVISIBLE)


# Decoration, supplier and setup language. Anything here in the Dietaries
# field means the transcription put the wrong paragraph in the wrong box.
# "cake" is deliberately absent: a gluten-free cake is a real dietary note.
_DIETARY_CONTAMINATION = re.compile(
    r"\bballoon|\bdecor|\bflower|\bfloral|\barch\b|\barchway|\bbackdrop|\bcentre\s?piece|\bcenter\s?piece|"
    r"\bcandle|\bsignage\b|\bsign\b|\bbanner|\blinen|\bnapkin|\bchair\s+(?:cover|sash)|\bfairy\s?lights?|"
    r"\bconfetti|\bgreenery|\bfoliage|\bprops?\b|\bhelium|\bneon\b|\bmarquee|\bplinth|\beasel|\briser\b|"
    r"\bgift\s+table|\bplace\s+cards?|\bseating\s+(?:chart|plan)|\btable\s+runner|\bphoto\s?booth|"
    r"\bstyl(?:ist|ing|ed)\b|\bset\s?-?\s?up\b|\bsetting\s+up\b|\bbump\s?-?\s?(?:in|out)\b|"
    r"\bdrop(?:ping|ped)?\s+off\b|\bdeliver(?:y|ing|ed|s)?\b|\bcollect(?:ion|ing)?\b|"
    r"\bsupplier|\bvendor|\bphotographer|\bvideographer|\bflorist|\bstylist|\bhire\b|"
    r"\binstall(?:er|ing|ation)?\b",
    re.IGNORECASE,
)

# The plain-word half of the vocabulary. The nut family is built separately
# below, by rule rather than by list, so nut and peanut are deliberately not
# here -- a maintainer widening allergen cover adds to _TREE_NUTS or trusts
# the "ends in nut" rule, and cannot accidentally give a nut word its own
# token and undo the canonicalisation.
_DIETARY_TOKENS = (
    "gluten", "coeliac", "celiac", "dairy", "lactose", "vegan", "vegetarian",
    "halal", "kosher", "shellfish", "seafood", "sesame", "egg", "soy", "pescatarian",
    "allerg", "anaphyla", "intoleran", "epipen", "fodmap",
)

# HOW A TOKEN IS MATCHED, and why it is neither a bare substring nor a
# plain \b on both sides. Two attempts have failed here, in opposite
# directions, and the rule below is shaped by both:
#
#   - `token in lowered` matched "nut" inside "minutes" and "egg" inside
#     "eggplant", so a current value of "Kitchen needs 20 minutes notice."
#     declared a nut allergy and every proposal that did not repeat it was
#     refused. Refused, not warned: these rules block, so the transcription
#     was stored and never shown to anybody.
#   - bounding every token with \b on both sides fixed that and broke the
#     spellings staff actually type -- "walnuts", "2xvegan", "glutenfree",
#     "nut-free", "eggless" all stopped declaring, so a real requirement
#     could be dropped in silence.
#
# So the boundary is explicit about what may sit beside a token rather than
# hoping one character class covers it:
#   LEFT  -- a non-letter, or a count like "2x" / "12x".
#   RIGHT -- a non-letter, an optional plural, or a "-free" / "less"
#            compound ("gluten free", "glutenfree", "nut-free", "eggless").
_LEFT = r"(?:(?<![a-z])|(?<=\dx))"
_TAIL = r"(?:[-\s]?free|[-\s]?less)?(?![a-z])"

# Stems, where any continuation is the same requirement: allergy/allergies/
# allergic/allergen, anaphylaxis/anaphylactic, intolerant/intolerance.
_DIETARY_STEMS = ("allerg", "anaphyla", "intoleran")
# Tokens whose compounds are a different word, not a plural.
_COMPOUND_SUFFIX = {"soy": r"(?:a|beans?|milk)?"}

# ANY word ending in "nut" is a nut: walnut, hazelnut, pinenut, groundnut,
# brazilnut, chestnut, coconut. A closed list of nut words was the previous
# attempt and it missed the ones nobody thought of, which on this field is a
# silent loss of safety data. The exceptions go the other way instead -- a
# short list of foods that merely end in the letters, where a wrong answer
# costs a needless refusal rather than a dropped allergy.
_NOT_A_NUT = ("butter", "dough", "do")          # butternut, doughnut, donut
# Tree nuts that do not end in "nut".
_TREE_NUTS = ("cashew", "almond", "pistachio", "macadamia", "pecan")


def _word_pattern(word: str) -> str:
    if word in _DIETARY_STEMS:
        return _LEFT + re.escape(word) + r"[a-z]*"
    return _LEFT + re.escape(word) + _COMPOUND_SUFFIX.get(word, r"(?:e?s)?") + _TAIL

# "nut" plus a plural only -- never the "es" the default allows, or
# "mi|nut|es" would read as a nut.
_NUT_PATTERN = (
    _LEFT + r"[a-z]*" + "".join(f"(?<!{p})" for p in _NOT_A_NUT) + r"nuts?" + _TAIL
)

# token -> compiled pattern. This is what declared_dietaries consults;
# _DIETARY_TOKENS above only seeds it. "peanut" is its own token AS WELL AS
# a nut: peanut is a legume and a distinct allergen -- the venue's own
# catalogue keeps it separate on MenuItem.contains_peanuts -- so swapping a
# peanut allergy for a tree-nut one has to read as a dropped requirement.
_DIETARY_PATTERNS: dict[str, re.Pattern] = {
    "nut": re.compile("|".join([_NUT_PATTERN] + [_word_pattern(w) for w in _TREE_NUTS]), re.IGNORECASE),
    "peanut": re.compile(_word_pattern("peanut"), re.IGNORECASE),
    **{token: re.compile(_word_pattern(token), re.IGNORECASE) for token in _DIETARY_TOKENS},
}

# First-person client voice. A run-sheet note has no narrator: "DJ from
# 8pm", not "we have organised a DJ from 8pm". The bare "i" branch
# refuses a following full stop so the venue's own "I.D. checks" wording
# -- which the RSA rule effectively asks for -- is not blocked by it.
_CLIENT_PROSE = re.compile(
    r"(?<!\w)(?:i(?![\w.])|i'm|i'll|i've|i'd|im|ive|we|we're|we'll|we've|we'd|weve|my|mine|myself|"
    r"our|ours|ourselves)(?!\w)",
    re.IGNORECASE,
)
# Requests and hopes: the client's voice even without a pronoun.
_CLIENT_REQUEST = re.compile(
    r"(?<!\w)(?:would\s+(?:like|love|prefer)|hoping|hope\s+to|please\s+can|could\s+you|can\s+you|"
    r"if\s+possible|wondering)(?!\w)",
    re.IGNORECASE,
)

# The default playlist line the wizard composes (see
# wizard_generation.MUSIC_TYPE_LINES["own_playlist"]) -- recognised by its
# distinctive parts rather than the whole sentence, so a lightly reworded
# copy still trips it.
# The RSA line an 18th's Event Order must carry. The term staff and the
# agreement both use is "RSA"; anything that says it satisfies this.
_RSA_LINE = re.compile(r"(?<!\w)r\.?s\.?a\.?(?!\w)|responsible\s+service\s+of\s+alcohol", re.IGNORECASE)


def mentions_rsa(text: str | None) -> bool:
    """Whether Special notes (or any text) carries the RSA line, by the one
    definition the proposal rules use. The floor's drift note asks this of
    a superseded version."""
    return bool(_RSA_LINE.search(normalise(text)))


@dataclass
class RuleViolation:
    code: str
    field: str | None
    message: str
    excerpt: str = ""


@dataclass
class RuleResult:
    violations: list[RuleViolation] = field(default_factory=list)
    # Non-blocking: shown to the reviewer, never a refusal. A warning is
    # for something true about the finished document that this proposal
    # did not cause and cannot be required to fix.
    warnings: list[RuleViolation] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.violations)

    @property
    def codes(self) -> list[str]:
        return [v.code for v in self.violations]

    @property
    def warning_codes(self) -> list[str]:
        return [v.code for v in self.warnings]

    def as_note(self) -> str:
        return " ".join(v.message for v in self.violations)

    def warning_note(self) -> str:
        return " ".join(v.message for v in self.warnings)


def _excerpt_text(text: str, width: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "\u2026"


def _excerpt(match: re.Match) -> str:
    return match.group(0).strip()[:120]


def declared_dietaries(text: str | None) -> set[str]:
    """Which dietary requirements a value declares, as canonical tokens.

    Whole words only -- see _DIETARY_PATTERNS. Every nut comes back as
    "nut", so the set difference that decides DROPS_DIETARY treats "walnut
    allergy" and "nut allergy" as one declaration; peanut answers as both
    "nut" and "peanut", so a peanut allergy cannot be swapped for a tree-nut
    one without the rule noticing.

    Read against both spellings of an invisible character -- deleted and
    replaced by a space -- because a paste can put one inside a word or
    between two, and the union is the only answer that cannot hide a
    declaration.
    """
    folded = _fold(text, _INVISIBLE).lower()
    spaced = _fold(text, _INVISIBLE_AS_SPACE).lower()
    return {
        token
        for token, pattern in _DIETARY_PATTERNS.items()
        if pattern.search(folded) or pattern.search(spaced)
    }


def looks_like_eighteenth(*, event_type: str | None, event_name: str | None, notes: str | None = None) -> bool:
    """Delegates to the one definition the rest of the system already uses
    (enquiry_classification.looks_like_18th, which the enquiry flag and
    the agreement's 18th appendix both run on) rather than keeping a
    second, divergent copy here. A bare "18" in an event name is a date,
    not a milestone -- that function requires birthday context."""
    return looks_like_18th(event_type or "", event_name or "", notes)


def validate(
    proposed: dict[str, str],
    *,
    current: dict[str, str] | None = None,
    event_type: str | None = None,
    event_name: str | None = None,
    notes: str | None = None,
    child_count: int = 0,
    legacy_music_entertainment: str | None = None,
) -> RuleResult:
    """Check field values against the house rules.

    `proposed` is what would be written. `current` is what the Event Order
    reads today, for the rules that can only be judged as a change:
    nothing is silently emptied, no declared dietary is dropped, and the
    RSA floor knows what the finished document would say.
    """
    try:
        return _validate(
            proposed,
            current=current or {},
            event_type=event_type,
            event_name=event_name,
            notes=notes,
            child_count=child_count,
            legacy_music_entertainment=legacy_music_entertainment,
        )
    except Exception:  # noqa: BLE001 -- fail closed
        logger.exception("Event Order proposal validation failed")
        return RuleResult(
            violations=[
                RuleViolation(RULES_ERROR, None, "The house rules could not be checked, so the proposal is withheld.")
            ]
        )


def _validate(
    proposed: dict[str, str],
    *,
    current: dict[str, str],
    event_type: str | None,
    event_name: str | None,
    notes: str | None,
    child_count: int,
    legacy_music_entertainment: str | None = None,
) -> RuleResult:
    result = RuleResult()

    legacy = normalise(legacy_music_entertainment).strip()
    if legacy and "music" in proposed:
        # After this write, is there an Entertainment value at all?
        entertainment_after = normalise(proposed.get("entertainment", current.get("entertainment"))).strip()
        music_after = normalise(proposed.get("music")).strip()
        if not entertainment_after and music_after != legacy:
            result.violations.append(
                RuleViolation(
                    LEGACY_MUSIC_SPLIT, "music",
                    "This Event Order still carries the older merged Music & entertainment value, and "
                    "approving Music alone would drop whatever part of it is entertainment. Propose "
                    "Music and Entertainment together (or approve Entertainment first), or make Music "
                    "carry the whole of it.",
                    _excerpt_text(legacy),
                )
            )

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
        text = normalise(value)
        if len(text) > MAX_FIELD_LENGTH:
            result.violations.append(
                RuleViolation(
                    FIELD_TOO_LONG, name,
                    f"{FIELD_LABELS[name]} is {len(text)} characters; the limit is {MAX_FIELD_LENGTH}. "
                    "This reads like a pasted email rather than a run-sheet note.",
                )
            )

        # Nothing is ever silently emptied. Removing what the Event Order
        # already says is a decision for a human on the field itself, not
        # something a transcription may do by proposing a blank.
        if not text.strip() and normalise(current.get(name)).strip():
            result.violations.append(
                RuleViolation(
                    ERASES_VALUE, name,
                    f"{FIELD_LABELS[name]} is empty but the Event Order currently has text in it. "
                    "A transcription may not blank a field: clear it by hand if that is really the intent.",
                )
            )

    # A declared dietary requirement can never quietly disappear.
    if "dietaries" in proposed:
        lost = declared_dietaries(current.get("dietaries")) - declared_dietaries(proposed["dietaries"])
        if lost:
            result.violations.append(
                RuleViolation(
                    DROPS_DIETARY, "dietaries",
                    "The Event Order currently declares " + ", ".join(sorted(lost)) +
                    " and this value does not. A declared dietary requirement is never dropped by a "
                    "transcription; if the client really withdrew it, change it by hand.",
                )
            )

    dietaries = normalise(proposed.get("dietaries"))
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
        text = normalise(value)
        match = _CLIENT_PROSE.search(text) or _CLIENT_REQUEST.search(text)
        if match:
            result.violations.append(
                RuleViolation(
                    CLIENT_PROSE, name,
                    f"{FIELD_LABELS[name]} is written in the client's voice. The Event Order is a run sheet: "
                    "state the fact, not the sentence they wrote.",
                    _excerpt(match),
                )
            )

    # OR, not AND. This read `child_count > 0 and looks_like_eighteenth(...)`,
    # which let a guest count remove a compliance line from a document:
    # child_count is 0 both when there are no minors AND when nobody was
    # asked for the split (enquiry_classification treats every attendee as
    # an adult when only a total is given), so an 18th booked without that
    # split never required the RSA line at all. Aaron, 2026-09-09: "That's
    # the licence, not a data quality issue... A client who under-reports or
    # leaves it blank shouldn't be able to remove a compliance line from a
    # document. The count should widen when the RSA line is needed, never
    # narrow it."
    #
    # So each trigger stands alone: an 18th needs the line whatever the
    # count says, and under-18s on any booking need it whatever the event
    # is called. The agreement's own 18th appendix has always fired on the
    # signal alone (document_generation.build_agreement_content) -- this
    # brings the Event Order's floor into line with it.
    is_eighteenth = looks_like_eighteenth(event_type=event_type, event_name=event_name, notes=notes)
    if is_eighteenth or child_count > 0:
        because = "This is an 18th" if is_eighteenth else "There are under-18s on this booking"
        effective_notes = normalise(proposed.get("special_notes", current.get("special_notes")))
        if not _RSA_LINE.search(effective_notes):
            if "special_notes" in proposed:
                # This value is rewriting Special notes and dropping the
                # line: that is a block, it is the thing being proposed.
                result.violations.append(
                    RuleViolation(
                        RSA_MISSING, "special_notes",
                        f"{because}, so Special notes has to carry the RSA line. Say it in the value you "
                        "are proposing.",
                    )
                )
            else:
                # The document lacks it, but this proposal did not cause
                # that and may be an allergy note that must not be
                # withheld over an unrelated policy gap. Surfaced, not
                # blocked (2026-09-06 review).
                result.warnings.append(
                    RuleViolation(
                        RSA_ABSENT_ON_DOCUMENT, "special_notes",
                        f"{because} and the Event Order's Special notes do not mention RSA. Worth adding "
                        "before it goes out.",
                    )
                )

    return result
