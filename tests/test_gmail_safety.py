"""Tests for app.services.gmail_safety -- see that module's docstring for
the incident this exists to prevent. All tests use a fake MailClient;
none touch a live mailbox, so these run in CI without credentials.
"""

import pytest

from app.services.gmail_safety import (
    CompleteThread,
    InsufficientEvidence,
    MessageId,
    ReplyStatusEvidence,
    ThreadFetchIncomplete,
    ThreadId,
    ThreadMessage,
    ThreadPage,
    assess_reply_status,
    fetch_complete_thread,
)

OUR_ADDRESS = "meantimehamilton@gmail.com"


def _msg(message_id: str, thread_id: str, sender: str, date: str, *, trash: bool = False) -> ThreadMessage:
    return ThreadMessage(
        message_id=MessageId(message_id),
        thread_id=ThreadId(thread_id),
        sender=sender,
        label_ids=frozenset({"TRASH"} if trash else {"INBOX"}),
        date=date,
    )


class FakeClient:
    """Maps thread_id -> list of pages. A page list of length 1 means
    "returns everything in one call, no more pages" -- next_page_token
    None on the last page. Raises KeyError for any thread_id not in the
    map, simulating a provider that doesn't recognise the ID (e.g. a
    message ID passed where a thread ID belongs)."""

    def __init__(self, pages_by_thread: dict[str, list[ThreadPage]]):
        self._pages_by_thread = pages_by_thread

    def get_thread_page(self, thread_id, *, page_token=None):
        pages = self._pages_by_thread[thread_id]  # KeyError on unknown ID -- deliberate
        index = 0 if page_token is None else int(page_token)
        page = pages[index]
        return page


# --- fetch_complete_thread: pagination and completeness ----------------------


def test_single_page_thread_returns_all_messages():
    messages = tuple(_msg(f"m{i}", "t1", OUR_ADDRESS, f"2026-08-{i:02d}") for i in range(1, 4))
    client = FakeClient({"t1": [ThreadPage(messages=messages, next_page_token=None)]})

    result = fetch_complete_thread(client, ThreadId("t1"))

    assert isinstance(result, CompleteThread)
    assert result.message_count == 3


def test_paginates_across_multiple_pages_without_losing_messages():
    page1 = ThreadPage(messages=(_msg("m1", "t1", OUR_ADDRESS, "2026-08-01"),), next_page_token="1")
    page2 = ThreadPage(messages=(_msg("m2", "t1", "client@example.com", "2026-08-02"),), next_page_token="2")
    page3 = ThreadPage(messages=(_msg("m3", "t1", OUR_ADDRESS, "2026-08-03"),), next_page_token=None)
    client = FakeClient({"t1": [page1, page2, page3]})

    result = fetch_complete_thread(client, ThreadId("t1"))

    assert result.message_count == 3
    assert {m.message_id for m in result.messages} == {MessageId("m1"), MessageId("m2"), MessageId("m3")}


def test_a_page_that_reports_no_further_token_is_trusted_as_the_boundary():
    """This is the module's real limitation, made explicit rather than
    hidden: completeness is only as good as the client's honesty about
    next_page_token. A client that silently caps messages (exactly the
    2026-08-13 incident: search_threads capped at 5 with no indication)
    and reports next_page_token=None on that capped page will fool this
    function too -- there is no independent count to check the page
    against. The actual fix for that specific failure is architectural:
    never use a client whose get_thread_page is backed by a
    capped-preview endpoint. This test documents that boundary rather
    than pretending it doesn't exist."""
    only_five_of_seven = tuple(_msg(f"m{i}", "t1", OUR_ADDRESS, f"2026-08-{i:02d}") for i in range(1, 6))
    lying_client = FakeClient({"t1": [ThreadPage(messages=only_five_of_seven, next_page_token=None)]})

    result = fetch_complete_thread(lying_client, ThreadId("t1"))

    assert result.message_count == 5  # wrong (7 really exist) -- see docstring above


def test_a_page_fetch_error_raises_incomplete_not_a_partial_result():
    class RaisingClient:
        def get_thread_page(self, thread_id, *, page_token=None):
            raise RuntimeError("rate limited")

    with pytest.raises(ThreadFetchIncomplete):
        fetch_complete_thread(RaisingClient(), ThreadId("t1"))


def test_pagination_that_never_terminates_raises_rather_than_looping_forever():
    class NeverEndingClient:
        def get_thread_page(self, thread_id, *, page_token=None):
            return ThreadPage(messages=(_msg("m", "t1", OUR_ADDRESS, "2026-08-01"),), next_page_token="always-more")

    with pytest.raises(ThreadFetchIncomplete):
        fetch_complete_thread(NeverEndingClient(), ThreadId("t1"), max_pages=5)


def test_unknown_thread_id_fails_loudly_not_with_partial_data():
    """Reproduces (with a fake) the ID-confusion scenario: a message ID
    passed where a thread ID belongs. The live tool tested against the
    real inbox on 2026-08-13 already failed cleanly here (see the audit
    report) -- this test pins that same contract so a future client
    swap can't silently regress into the "plausible partial thread"
    failure mode described in the incident."""
    client = FakeClient({"t1": [ThreadPage(messages=(), next_page_token=None)]})

    with pytest.raises(ThreadFetchIncomplete):
        fetch_complete_thread(client, ThreadId("not-a-real-thread-id"))


def test_empty_thread_id_rejected_before_any_client_call():
    class ExplodingClient:
        def get_thread_page(self, thread_id, *, page_token=None):
            raise AssertionError("should never be called with an empty thread_id")

    with pytest.raises(ValueError):
        fetch_complete_thread(ExplodingClient(), ThreadId(""))


# --- assess_reply_status: the status-claim gate -------------------------------


def _evidence(*, primary_messages, trash_checked=True, trash_messages=None, alt_checked=True, alt_email=None):
    primary = CompleteThread(thread_id=ThreadId("t1"), messages=tuple(primary_messages))
    trash = CompleteThread(thread_id=ThreadId("t1"), messages=tuple(trash_messages)) if trash_messages else None
    return ReplyStatusEvidence(
        primary_thread=primary,
        trash_checked=trash_checked,
        trash_thread=trash,
        alternate_email_checked=alt_checked,
        alternate_email=alt_email,
    )


def test_reply_status_raises_if_trash_not_checked():
    evidence = _evidence(primary_messages=[_msg("m1", "t1", OUR_ADDRESS, "2026-08-01")], trash_checked=False)
    with pytest.raises(InsufficientEvidence):
        assess_reply_status(evidence, our_addresses=frozenset({OUR_ADDRESS}))


def test_reply_status_raises_if_alternate_email_not_checked():
    evidence = _evidence(primary_messages=[_msg("m1", "t1", OUR_ADDRESS, "2026-08-01")], alt_checked=False)
    with pytest.raises(InsufficientEvidence):
        assess_reply_status(evidence, our_addresses=frozenset({OUR_ADDRESS}))


def test_reply_status_true_when_client_message_present():
    evidence = _evidence(primary_messages=[
        _msg("m1", "t1", OUR_ADDRESS, "2026-08-01"),
        _msg("m2", "t1", "client@example.com", "2026-08-02"),
    ])
    verdict = assess_reply_status(evidence, our_addresses=frozenset({OUR_ADDRESS}))
    assert verdict.has_client_reply is True


def test_reply_status_false_when_only_our_own_addresses_sent():
    """The genuine "never replied" case -- must still be reachable, not
    just always-true, once real evidence has actually been checked."""
    evidence = _evidence(primary_messages=[
        _msg("m1", "t1", OUR_ADDRESS, "2026-08-01"),
        _msg("m2", "t1", OUR_ADDRESS, "2026-08-05"),
    ])
    verdict = assess_reply_status(evidence, our_addresses=frozenset({OUR_ADDRESS}))
    assert verdict.has_client_reply is False


def test_reply_status_finds_a_reply_that_only_exists_in_trash():
    """Direct test of the Trash requirement: the primary thread alone
    looks unanswered, but the client's reply landed in Trash."""
    evidence = _evidence(
        primary_messages=[_msg("m1", "t1", OUR_ADDRESS, "2026-08-01")],
        trash_messages=[_msg("m2", "t1", "client@example.com", "2026-08-02", trash=True)],
    )
    verdict = assess_reply_status(evidence, our_addresses=frozenset({OUR_ADDRESS}))
    assert verdict.has_client_reply is True


def test_reply_status_latest_message_spans_primary_and_trash():
    evidence = _evidence(
        primary_messages=[_msg("m1", "t1", OUR_ADDRESS, "2026-08-01")],
        trash_messages=[_msg("m2", "t1", "client@example.com", "2026-08-09", trash=True)],
    )
    verdict = assess_reply_status(evidence, our_addresses=frozenset({OUR_ADDRESS}))
    assert verdict.latest_message.message_id == MessageId("m2")


def test_reply_status_sender_comparison_is_case_insensitive():
    """A real case seen in the live inbox: the same address appears as
    both Joanned@... and joanned@... across messages in one thread."""
    evidence = _evidence(primary_messages=[
        _msg("m1", "t1", OUR_ADDRESS.upper(), "2026-08-01"),
    ])
    verdict = assess_reply_status(evidence, our_addresses=frozenset({OUR_ADDRESS}))
    assert verdict.has_client_reply is False
