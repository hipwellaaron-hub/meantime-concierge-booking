"""The document editor is chosen by an explicit mapping, not by "agreement,
else the other one".

_edit_form_response picked its template with a two-way conditional whose
else-branch was the Event Order editor. Every DocumentType it had never
heard of would have got a run-sheet form, silently -- a third type is
exactly the moment somebody is adding code and least expecting a template
to guess. An unknown type is a bug in whatever added it, and it says so.
"""
import pytest

from app.api.admin_bookings import _editor_template_for
from app.models.document import DocumentType


def test_each_real_type_has_its_own_editor():
    assert _editor_template_for(DocumentType.agreement) == "admin/document_edit_agreement.html"
    assert _editor_template_for(DocumentType.beo) == "admin/document_edit_beo.html"


def test_an_unknown_type_is_refused_rather_than_handed_the_event_order_editor():
    with pytest.raises(ValueError, match="no editor is registered"):
        _editor_template_for("run_sheet_v2")


def test_every_document_type_is_mapped():
    """A new member of the enum with no editor should fail here, in a
    test, not in a browser."""
    for doc_type in DocumentType:
        assert _editor_template_for(doc_type).startswith("admin/document_edit_")
