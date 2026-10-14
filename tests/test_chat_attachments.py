from __future__ import annotations

import pytest

from agent_core.chat_attachments import ChatAttachmentStore, InvalidImageError
from agent_core.contracts import Scope


PNG = b"\x89PNG\r\n\x1a\n" + b"payload"


def test_upload_claim_and_delete_are_scope_bound(tmp_path):
    store = ChatAttachmentStore(tmp_path / "attachments.db", tmp_path / "files", ttl_seconds=60)
    scope = Scope("alice", "web", "conversation-1", "companion")
    other = Scope("bob", "web", "conversation-1", "companion")

    attachment = store.upload(scope, "photo.png", "image/png", PNG)

    assert attachment.size_bytes == len(PNG)
    assert store.get_for_scope(other, attachment.attachment_id) is None
    claimed = store.claim(scope, attachment.attachment_id)
    assert claimed.path.read_bytes() == PNG
    store.delete(scope, attachment.attachment_id)
    assert not claimed.path.exists()
    assert store.get_for_scope(scope, attachment.attachment_id) is None


def test_failed_request_can_release_attachment_for_retry(tmp_path):
    store = ChatAttachmentStore(tmp_path / "attachments.db", tmp_path / "files", ttl_seconds=60)
    scope = Scope("alice", "web", "conversation-1", "companion")
    attachment = store.upload(scope, "photo.png", "image/png", PNG)

    store.claim(scope, attachment.attachment_id)
    store.release(scope, attachment.attachment_id)
    retried = store.claim(scope, attachment.attachment_id)

    assert retried.attachment_id == attachment.attachment_id


def test_upload_rejects_invalid_signature_and_expired_files(tmp_path):
    store = ChatAttachmentStore(tmp_path / "attachments.db", tmp_path / "files", ttl_seconds=0)
    scope = Scope("alice", "web", "conversation-1", "companion")

    with pytest.raises(InvalidImageError):
        store.upload(scope, "photo.png", "image/png", b"not an image")

    attachment = store.upload(scope, "photo.png", "image/png", PNG)
    assert store.cleanup_expired() == 1
    assert store.get_for_scope(scope, attachment.attachment_id) is None
