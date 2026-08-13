"""Structural protection against silent email-thread truncation and
thread/message ID confusion.

Incident, 2026-08-13: a thread-search style call against the live
Meantime Hamilton inbox returned only the 5 most recent-looking messages
of every conversation it listed, with no count and no truncation flag --
indistinguishable from a genuinely short thread. Because the *newest*
messages were the ones dropped, every affected conversation looked like
it had gone cold weeks earlier than it had. Reproduced live and
confirmed twice: one thread showed 5 of 7 real messages, another showed
5 of 6 -- the missing message in the second case was a client reply
still sitting unanswered. A related failure: Gmail's own ID scheme makes
a thread's ID identical to its first message's ID, so a thread ID and a
message ID are the same *shape* of opaque string with no way to tell
them apart by inspection.

Concierge has no Gmail (or other mail) integration today -- confirmed by
grep, not assumption. This module exists so that whenever one is built,
it is not possible to wire it up the way the tooling that produced the
incident was wired up. It is deliberately provider-agnostic (a
MailClient Protocol, not a concrete SDK), so it can be fully unit-tested
now with a fake client and adopted later by a real one without every
caller having to remember the rule.

The invariant this module exists to enforce: no code may derive a
booking's state, a "client hasn't replied" claim, or a drafted reply
from anything other than a CompleteThread -- and a CompleteThread can
only be constructed by fetch_complete_thread, which either proves
completeness or raises. There is no partial variant.
"""

from __future__ import annotations

import dataclasses
from typing import NewType, Protocol

# Distinct types, not two `str` fields sharing a name -- the whole point
# is that these must never be silently interchangeable in code that
# stores, passes, or reuses one. NewType gives real protection under a
# type checker (mypy will flag `fetch_complete_thread(some_message_id)`
# if `some_message_id: MessageId`); it is not runtime-enforceable on its
# own because the underlying string is genuinely indistinguishable by
# inspection -- see MailClient's contract below for the runtime half of
# this guarantee.
ThreadId = NewType("ThreadId", str)
MessageId = NewType("MessageId", str)

TRASH_LABEL = "TRASH"


@dataclasses.dataclass(frozen=True)
class ThreadMessage:
    message_id: MessageId
    thread_id: ThreadId
    sender: str
    label_ids: frozenset[str]
    date: str  # ISO 8601 as returned by the provider; callers parse if they need to compare

    @property
    def is_in_trash(self) -> bool:
        return TRASH_LABEL in self.label_ids


@dataclasses.dataclass(frozen=True)
class CompleteThread:
    """The only shape any status-claiming code may read. Never construct
    this directly outside fetch_complete_thread -- there is deliberately
    no way to build one from a search/list response's embedded preview
    messages, because that preview is exactly what produced the
    incident this module exists to prevent."""

    thread_id: ThreadId
    messages: tuple[ThreadMessage, ...]

    @property
    def message_count(self) -> int:
        return len(self.messages)

    @property
    def latest_message(self) -> ThreadMessage | None:
        return max(self.messages, key=lambda m: m.date, default=None)


@dataclasses.dataclass(frozen=True)
class ThreadPage:
    messages: tuple[ThreadMessage, ...]
    next_page_token: str | None


class ThreadFetchIncomplete(Exception):
    """Raised whenever completeness cannot be proven -- a rate limit, a
    pagination failure, a mid-page provider error, an ID the provider
    doesn't recognise as a thread root, or pagination that never
    terminates. Deliberately never caught and silently downgraded to a
    partial result anywhere in this module. A caller that wants to
    surface a best-effort partial view to a human must catch this
    itself and mark whatever it produces with an explicit [REVIEW]
    marker -- see the module docstring; a partial read must never be
    allowed to look like a complete one.
    """


class MailClient(Protocol):
    """Minimal surface this module needs from a real mail provider
    client. A production adapter wraps whatever SDK/MCP tool is chosen;
    tests use a fake implementing this same Protocol, so the pagination
    and completeness logic below is fully tested without live
    credentials.

    Contract: get_thread_page must raise (not return a partial or empty
    page) if thread_id does not identify a real thread root -- in
    particular, it must not silently accept a non-root message ID and
    return whatever it finds. This is the runtime half of the
    ThreadId/MessageId separation; the type distinction above is the
    development-time half.
    """

    def get_thread_page(self, thread_id: ThreadId, *, page_token: str | None = None) -> ThreadPage: ...


def fetch_complete_thread(client: MailClient, thread_id: ThreadId, *, max_pages: int = 20) -> CompleteThread:
    """The only sanctioned way to read a thread for the purpose of
    deriving state. Pages until the provider reports no further page
    token, so the caller can always state exactly how many messages
    were retrieved. Never trusts a single page -- e.g. a search result's
    embedded message preview -- as complete; there is no code path here
    that accepts one.

    Raises ThreadFetchIncomplete, never returns a partial CompleteThread,
    if any page fetch raises or if pagination does not terminate within
    max_pages (a safety valve against a provider whose page_token chain
    never ends -- not an expected outcome for a real thread).
    """
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("thread_id must be a non-empty string")

    messages: list[ThreadMessage] = []
    page_token: str | None = None
    for _ in range(max_pages):
        try:
            page = client.get_thread_page(thread_id, page_token=page_token)
        except Exception as exc:
            raise ThreadFetchIncomplete(f"thread {thread_id!r} fetch failed: {exc}") from exc
        messages.extend(page.messages)
        if page.next_page_token is None:
            return CompleteThread(thread_id=thread_id, messages=tuple(messages))
        page_token = page.next_page_token

    raise ThreadFetchIncomplete(
        f"thread {thread_id!r} did not finish paginating within {max_pages} pages -- "
        "refusing to treat this as complete rather than guessing"
    )


@dataclasses.dataclass(frozen=True)
class ReplyStatusEvidence:
    """Proof that a 'has this client replied' / 'has this gone quiet'
    claim was actually checked against everything it needs to be, not
    just the obvious inbox thread. Both a genuine reply sitting in Trash
    and a client replying from a second email address produced real
    false "unanswered" conclusions in live use -- a claim assembled
    without checking both is a guess with a plausible shape, not
    evidence.
    """

    primary_thread: CompleteThread
    trash_checked: bool
    trash_thread: CompleteThread | None  # only meaningful if trash_checked is True
    alternate_email_checked: bool
    alternate_email: str | None  # the second address actually checked, if one was known to check


class InsufficientEvidence(Exception):
    """Raised by assess_reply_status when trash and/or a second email
    address were not checked. There is no default or best-guess path
    here on purpose -- "no reply" must be demonstrated, never assumed
    because nothing else was visible."""


@dataclasses.dataclass(frozen=True)
class ReplyStatusVerdict:
    has_client_reply: bool
    latest_message: ThreadMessage | None
    checked_trash: bool
    checked_alternate_email: str | None


def assess_reply_status(evidence: ReplyStatusEvidence, *, our_addresses: frozenset[str]) -> ReplyStatusVerdict:
    """The only sanctioned way to answer "has the client replied" /
    "has this gone quiet". Raises InsufficientEvidence rather than
    guessing if the required checks weren't done.

    our_addresses identifies which senders are "us", not "the client" --
    comparing against the most recent message's sender is not enough
    (several of our own addresses have sent from this inbox; see
    app.services.policy.VENUE_CONTACT_EMAIL and the historical
    hello@meantime.com.au address). A reply is any message from a
    sender not in this set.
    """
    if not evidence.trash_checked:
        raise InsufficientEvidence("cannot assess reply status without checking Trash")
    if not evidence.alternate_email_checked:
        raise InsufficientEvidence("cannot assess reply status without checking for a second email address")

    threads = [evidence.primary_thread]
    if evidence.trash_thread is not None:
        threads.append(evidence.trash_thread)

    all_messages = [m for t in threads for m in t.messages]
    normalized_ours = {addr.lower() for addr in our_addresses}
    has_client_reply = any(m.sender.lower() not in normalized_ours for m in all_messages)
    latest = max(all_messages, key=lambda m: m.date, default=None)

    return ReplyStatusVerdict(
        has_client_reply=has_client_reply,
        latest_message=latest,
        checked_trash=evidence.trash_checked,
        checked_alternate_email=evidence.alternate_email if evidence.alternate_email_checked else None,
    )
