"""Which fields of a document a PERSON wrote, recorded rather than guessed.

Regenerating a document rebuilds its content from the booking. For
everything the booking computes -- the timeline, the food order, totals,
the room -- that is the whole point. For the fields a person writes in
their own words it is destruction: an approved allergy note, a
hand-negotiated contract clause. Deciding which is which is the question
this module answers, and it answers it from a record.

The record is a POSITIVE set: `content["_authored"]` lists the keys a
human has written. It starts absent, and any writer can create it -- but
only by recording an actual name. No function here turns "no record" into
an empty record, because those two states send a reader in opposite
directions: absent means "go and read the audit trail", empty means
"nothing here is a person's, help yourself".

That direction is deliberate, and it is the second attempt. The first
recorded the opposite -- the keys the GENERATOR produced -- so only the
generator could ever initialise it, a document written before the feature
could never acquire one and stayed permanently "unknowable", and a
document nobody stamped claimed every field as human (2026-09-07 review,
reverted). Recording what a person did is smaller, initialisable by
whoever does it, and says nothing about content nobody has touched.

HOW A CALLER USES THIS. Two rules, both learned the hard way:

  1. Give `record` the content you are about to STORE, never a fragment.
     A partial dict -- `update_content_fields`' `changes`, say -- carries
     no record of its own, so recording against it produces a record
     naming only those keys, and merging that over the stored content
     erases every other name. Merge first, record second. `carry` is for
     the other shape: moving a record onto freshly rebuilt content.
  2. Call `record`/`forget`/`carry` LAST, after any other edit, and assign
     the result. All three deep-copy, which protects a nested value you
     edit on the RESULT; none can protect one you edited on the loaded
     object before calling, because by then the damage is done. This
     includes `carry`, whose copy is of `fresh` -- and `carry` is the one
     a regenerate actually reaches for.

Rule 2 exists because `document.content` is a JSONB column with no
mutation tracking, and SQLAlchemy decides whether to emit an UPDATE by
comparing the attribute against the value it loaded, by EQUALITY. So the
dict you assign must compare unequal to the loaded one. Mutating in place
fails that; an early draft of this module did exactly that and
recommended it. A SHALLOW copy fails it too, but only where the top level
does not change -- the second edit to an already-recorded field, and a
`forget` of a name that was not in the record -- and a later draft
claimed the shallow copy always failed, which was false. Every one of
those cases is now a test, and so is the ordering rule.

Reads tolerate anything; writes do not. `authored` and `has_record` take
whatever a JSONB column can hold and degrade to "no record", because a
malformed row must not be the thing that breaks a document page. `record`
and `forget` refuse content that is not a dict, and refuse an unusable
field name, because that is a caller with a bug. `carry` sits on both
sides: it reads `previous` as tolerantly as any other stored row, and
refuses a `fresh` that is not a dict.

What this module does NOT do:

  - it does not decide whether a value may be overwritten. Absent
    authorship means "no record on THIS content" -- true of a document
    written before the record existed, and equally true of content the
    generator has just built. Telling those apart needs the booking's
    audit trail, which is the caller's;
  - it does not judge the text. `changed_fields` compares two values for
    equality, but whether a value looks like a generated placeholder is a
    separate question with a separate answer;
  - it does not decide WHICH fields a person's words belong in. Every
    caller of `changed_fields` names its own candidates, because "this key
    holds prose somebody typed" is knowable at the call site and a guess
    anywhere else.

Nothing consumes this yet. It is the record; the readers come next.
"""

import copy
from collections.abc import Iterable

# Stored inside the document's content dict. Underscore-prefixed, like
# `_reference`, so it travels with the content and the client-facing
# templates -- which address content by name -- never render it.
AUTHORED_KEY = "_authored"


def _is_field_name(key: object) -> bool:
    """A real content field name. Whitespace is not one: a form that posts
    a space, or a caller stripping input, would otherwise write a
    permanent junk member that reads as real authorship."""
    return isinstance(key, str) and bool(key.strip()) and not key.startswith("_")


def _names_to_write(keys: Iterable[str]) -> set[str]:
    """The names a writer asked to record, refusing anything unusable.

    Metadata keys are dropped rather than refused: a caller legitimately
    passes a whole content dict's keys, and `_reference` appearing there
    is not a mistake. Anything that is not a usable name at all IS a
    mistake, and a caller with a bug should hear about it rather than
    have this guess.

    The reason is the caller's bug, not the resulting record: `record(content,
    [])` legitimately records nothing, so "it would leave a record saying
    nobody wrote anything" cannot be the justification.
    """
    if isinstance(keys, (str, bytes)):
        # A string is iterable, so record(content, "dietaries") would
        # record seven letters and no field. The likeliest typo at any
        # call site, and it must not pass quietly. Named by the type
        # actually passed, or the message sends a caller who passed bytes
        # looking for a str they never wrote.
        raise TypeError(
            f"keys must be a collection of field names, not a single {type(keys).__name__}"
        )
    try:
        keys = list(keys)
    except TypeError as exc:
        raise TypeError(f"keys must be a collection of field names; got {keys!r}") from exc
    unusable = [k for k in keys if not isinstance(k, str) or not k.strip()]
    if unusable:
        raise TypeError(f"field names must be non-empty, non-blank strings; got {unusable!r}")
    return {k for k in keys if _is_field_name(k)}


def _stored(content: object) -> set[str] | None:
    """The recorded names, or None when there is no record to read.

    A member that is not a usable name is dropped, not fatal: discarding
    a whole record over one bad entry would silently forget the real
    names beside it, and forgetting a person's authorship is the failure
    this module exists to prevent.

    But a record whose members are ALL unusable is not a record. Reading
    it as one would say "authorship was recorded and nobody wrote
    anything", which sends a reader the opposite way from "no record" --
    the first invites an overwrite, the second sends it to the audit
    trail. An explicitly empty record is still a record.

    Only a list is a record: that is what JSONB returns and what `record`
    writes. A set cannot be stored at all (psycopg raises at flush), and
    reading one would invite a caller to build one by hand.
    """
    if not isinstance(content, dict):
        return None
    stored = content.get(AUTHORED_KEY)
    if not isinstance(stored, list):
        return None
    names = {k for k in stored if _is_field_name(k)}
    if not names and len(stored) > 0:
        return None
    return names


def authored(content: object) -> set[str]:
    """The keys a person has written, as far as anything has recorded.

    An empty set means no human write has been RECORDED -- not that none
    happened. See the module docstring on why that is not the same
    question as whether a value may be overwritten.

    Names are returned as recorded, including any for a field the content
    no longer holds. Dropping those would make this subtractive, and a
    caller holding a partial dict would silently erase authorship it
    could not see. The cost is that a name can outlive its field: if the
    key is later re-created by the generator, the record still calls it a
    person's, so a reader keeps a generated value and the audit line says
    a human's words were kept when none were. `forget` is how a caller
    that knows a field has been handed back to the generator says so.
    """
    return _stored(content) or set()


def has_record(content: object) -> bool:
    """Whether authorship was ever recorded on this content at all.

    The difference between "a person wrote nothing here" and "nobody has
    ever recorded anything here" is exactly what separates freshly
    generated content from content that predates the record, so it has to
    be askable.
    """
    return _stored(content) is not None


def _comparable(value: object) -> object:
    """A value normalised for COMPARISON only -- never stored.

    A browser submits a textarea's line breaks as CRLF while the stored
    JSONB holds LF, so a byte comparison calls every multi-line field
    changed on every save. Verified rather than assumed: a real form
    submission of "line one\\nline two" serialises as
    `t=line+one%0D%0Aline+two`, CR LF (13, 10). The `FormData` API value
    of the same textarea is plain LF, which is the trap -- checking it
    that way says CRLF never arrives and this normalisation is dead code.

    Applied recursively, because the agreement's `terms_sections` is a
    list of dicts whose `body` carries the prose.
    """
    if isinstance(value, str):
        return value.replace("\r\n", "\n").replace("\r", "\n")
    if isinstance(value, dict):
        return {k: _comparable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_comparable(v) for v in value]
    return value


def _written_value(value: object, placeholders: frozenset = frozenset()) -> object:
    """A field value reduced to what a person can be said to have written.

    Absent, None, "", "   ", `[]` and `{}` are one state: nothing. Without
    this, a form that writes `content["music"] = music.strip() or None`
    over a key that was not there records the person as the author of an
    empty field, and the regenerate then declines to fill in a value they
    never withheld. The empty container belongs in the same state for the
    same reason -- an empty list of terms sections is not a clause
    somebody wrote -- and leaving it out made the rule true of strings
    only, which would come apart the first time a container-valued field
    joined the protected set.
    """
    normalised = _comparable(value)
    if normalised is None:
        return ""
    if isinstance(normalised, str) and normalised in placeholders:
        # The generator's "nothing was captured here" reads as nothing on
        # BOTH sides of the comparison. Only on the incoming side and
        # clearing a placeholder to blank counts as a write, so the record
        # claims a person authored an empty field and the regenerate stops
        # filling in the very value the placeholder was asking for.
        return ""
    if isinstance(normalised, str) and not normalised.strip():
        return ""
    if isinstance(normalised, (list, dict)) and not normalised:
        return ""
    return normalised


def changed_fields(
    stored: object,
    incoming: object,
    *,
    candidates: Iterable[str],
    placeholders: Iterable[str] = (),
) -> set[str]:
    """Which of `candidates` `incoming` actually changes against `stored`.

    The names to record. A staff form re-posts every field it renders,
    prefilled, so "the form wrote this key" is not evidence a person wrote
    anything -- one save would otherwise claim all ten free-text fields as
    hand-written and freeze the lot against regeneration. Only a value
    that genuinely differs counts.

    `candidates` is required and keyword-only: it is the caller's
    declaration of which keys hold a person's prose. Passing the whole
    incoming dict's keys would record `vendors` and `event_timeline` --
    machine-derived values a regenerate MUST rebuild -- as somebody's
    words, and the guard built to save an allergy note would start
    preserving a stale vendor list instead.

    `placeholders` are the values the GENERATOR writes when nothing was
    captured. A field whose new value is one of them is never recorded,
    however different it is from what was there: nobody authored the
    generator's own words. The staff form is what makes this concrete --
    it writes `content["dietaries"] = dietaries.strip() or NO_DIETARIES`,
    so clearing the box substitutes the placeholder, and without this the
    record would claim a person wrote "No dietary requirements declared"
    and a later regenerate would preserve it as theirs. Losing the
    previous value is a separate matter, and one the regenerate screen
    already warns about.

    Keys absent from `incoming` are not considered: a partial `changes`
    dict says nothing about the keys it omits.

    See `differing_fields` for the neighbouring question -- what a save
    would OVERWRITE -- which is not the same and must not be answered with
    this function.
    """
    return _compare(
        stored, incoming, candidates=candidates, placeholders=placeholders, skip_placeholder_writes=True
    )


def differing_fields(
    stored: object,
    incoming: object,
    *,
    candidates: Iterable[str],
    placeholders: Iterable[str] = (),
) -> set[str]:
    """Which of `candidates` `incoming` would REPLACE on `stored`.

    A different question from `changed_fields`, and the distinction is the
    whole reason both exist. This one asks what a save would overwrite --
    the question a screen asks when it has to tell somebody what they are
    about to discard. `changed_fields` asks what a PERSON can be said to
    have written, which is the question the record answers.

    They differ in exactly one case: writing a generated placeholder over
    somebody's real text. Nobody authored the generator's sentence, so it
    is not a change in the authorship sense -- but it certainly replaces
    what was there, and a screen that stayed quiet about it would let real
    text be swapped for "[REVIEW] add bar structure" with nothing said
    (review of 84916a7, where this function's absence did exactly that).

    Comparison is CRLF-normalised and treats absent, None, blank and empty
    containers as one state. A STORED placeholder is read as nothing too,
    which is narrower than it sounds: it means blanking a placeholder is
    not reported, because nothing of anybody's is lost. Writing real words
    over a stored placeholder IS reported -- the value on the document is
    about to change and the screen should say so.
    """
    return _compare(stored, incoming, candidates=candidates, placeholders=placeholders)


def _compare(stored, incoming, *, candidates, placeholders, skip_placeholder_writes=False) -> set[str]:
    if not isinstance(incoming, dict):
        raise TypeError(f"incoming content must be a dict; got {type(incoming).__name__}")
    names = _names_to_write(candidates)
    if isinstance(placeholders, (str, bytes)):
        raise TypeError("placeholders must be a collection of values, not a single string")
    # Text only. The generator's placeholders are sentences, and a field
    # value can be a list -- terms_sections is -- which is unhashable and
    # would raise on the membership test below rather than simply not
    # matching.
    nobody_wrote = frozenset(_comparable(p) for p in placeholders if isinstance(p, str))
    before = stored if isinstance(stored, dict) else {}
    changed = set()
    for name in names:
        if name not in incoming:
            continue
        submitted = _comparable(incoming[name])
        if skip_placeholder_writes and isinstance(submitted, str) and submitted in nobody_wrote:
            # Whatever was there before, nobody authored the generator's
            # own sentence. That it REPLACES something is a real fact, and
            # differing_fields is what reports it.
            continue
        if _written_value(before.get(name), nobody_wrote) != _written_value(incoming[name], nobody_wrote):
            changed.add(name)
    return changed


def _checked_names(content: object, keys: Iterable[str]) -> set[str]:
    """Validate both arguments BEFORE any copying, so a caller's bug costs
    an exception rather than a deep copy of a whole document. Returns only
    the names: `content` comes back unchanged, and returning it read as
    though this normalised or copied it, which it does not."""
    if not isinstance(content, dict):
        raise TypeError(f"content must be a dict to record authorship on; got {type(content).__name__}")
    return _names_to_write(keys)


def _with_record(content: dict, names: set[str]) -> dict:
    """A deep copy of `content` whose record is exactly `names`.

    The one place the record is written. Sorted, so that re-writing the
    same names produces a dict EQUAL to the loaded one and SQLAlchemy
    emits nothing -- a no-op save must not rewrite the row.
    """
    updated = copy.deepcopy(content)
    updated[AUTHORED_KEY] = sorted(names)
    return updated


def record(content: dict, keys: Iterable[str]) -> dict:
    """A deep copy of `content` with `keys` also recorded as a person's.

    Give it the content you are about to store, not a fragment -- see the
    module docstring, rule 1.

    Purely additive: it never removes a name. Creating the record when
    there was not one is what lets a document written before this feature
    acquire real, per-field authorship the first time somebody writes to
    it.

    But it will not create an EMPTY one. With no usable name to record and
    no record already present, this returns the content unchanged, because
    writing `_authored: []` there would turn "nobody has ever recorded
    here, go and read the audit trail" into "recorded, and nobody wrote
    anything, help yourself" -- on a document that predates the feature,
    where a declared allergy may be sitting in a field a person typed.
    That is the reversal the first attempt was reverted for, and an empty
    `changes` dict reaching the writer is enough to trigger it, so it is
    refused here rather than left to a caller to remember.
    """
    names = _checked_names(content, keys)
    if not names and not has_record(content):
        return copy.deepcopy(content)
    # Read the prior record through authored(), which keeps the real names
    # beside an unreadable member AND drops the unusable ones -- so a
    # record carrying junk is cleaned as it is rewritten, rather than
    # carrying that junk forward for ever.
    return _with_record(content, authored(content) | names)


def forget(content: dict, keys: Iterable[str]) -> dict:
    """A deep copy of `content` with `keys` no longer recorded as a person's.

    For the case where a human value is deliberately replaced by a
    regenerated one: the new value is the generator's, and saying so is
    what stops one regenerate laundering the next one's decision.

    Content with no record is returned unchanged (but still copied) --
    forgetting is not a write, and it must not conjure a record that then
    reads as "a person wrote nothing here".
    """
    dropping = _checked_names(content, keys)
    stored = _stored(content)
    if stored is None:
        return copy.deepcopy(content)
    return _with_record(content, stored - dropping)


def carry(fresh: dict, *, previous: object) -> dict:
    """A deep copy of freshly rebuilt `fresh` carrying `previous`'s record.

    A rebuild throws the content away and builds it again, so the record
    goes with it unless somebody moves it. This is that move. Rule 2
    applies here as much as to `record` and `forget`: the copy protects a
    nested value edited on the RESULT, not one already edited on `fresh`.

    `previous` is keyword-only. Both arguments are content dicts, so a
    swap would raise nothing and quietly return the PREVIOUS version's
    text as the rebuild's -- discarding the regenerate entirely while
    every log line says it succeeded. Content-to-be-written comes first
    here, as in `record` and `forget`; the keyword makes the other order
    unspellable rather than merely discouraged.

    It does NOT conjure a record: if the previous version never had one,
    the result has none either. Doing it the obvious way --
    `record(fresh, authored(previous))` -- would once have turned "this
    document predates the record" into "recorded, and nobody wrote
    anything"; `record` now refuses that itself, and this stays explicit
    because an empty record on `previous` must still CARRY as an empty
    record, which is a different answer from no record at all.
    """
    if not isinstance(fresh, dict):
        raise TypeError(f"fresh content must be a dict; got {type(fresh).__name__}")
    if not has_record(previous):
        return copy.deepcopy(fresh)
    return _with_record(fresh, authored(fresh) | authored(previous))
