"""PrConversation 도메인 단위 테스트.

핵심: PR-1 의 conversation 컨텍스트 표현. 빈 대화 / kind 별 entry 가 모두 보존되는지 lock.
"""
from gemini_review.domain import ConversationEntry, PrConversation


def _entry(**overrides: object) -> ConversationEntry:
    """기본값 채운 entry helper — 테스트가 관심 있는 필드만 override."""
    defaults: dict[str, object] = {
        "kind": "review",
        "author_login": "alice",
        "is_bot": False,
        "is_pr_author": False,
        "submitted_at": "2026-04-26T10:00:00Z",
        "body": "default body",
    }
    defaults.update(overrides)
    return ConversationEntry(**defaults)  # type: ignore[arg-type]


def test_empty_conversation_is_empty() -> None:
    """기본 PrConversation 은 entry 0 건 — `is_empty()` True (프롬프트 섹션 생략 신호)."""
    conv = PrConversation()
    assert conv.is_empty() is True
    assert conv.entries == ()


def test_non_empty_conversation_preserves_entries() -> None:
    """entries tuple 이 그대로 보존됨 — 프롬프트 렌더에 정확한 항목이 들어가도록."""
    e1 = _entry(kind="review", body="첫 review")
    e2 = _entry(kind="line_comment", path="a.py", line=10, comment_id=1, body="라인 코멘트")
    conv = PrConversation(entries=(e1, e2))

    assert conv.is_empty() is False
    assert len(conv.entries) == 2
    assert conv.entries[0] is e1
    assert conv.entries[1] is e2


def test_conversation_entry_optional_fields_default_to_none() -> None:
    """state / comment_id / path / line / in_reply_to_id 는 옵션 — None default 가 자연 매핑.

    예: review 는 path/line 없음, line_comment 는 state 없음 등 — 한 dataclass 에서 모두 표현.
    """
    e = _entry()
    assert e.state is None
    assert e.comment_id is None
    assert e.path is None
    assert e.line is None
    assert e.in_reply_to_id is None
