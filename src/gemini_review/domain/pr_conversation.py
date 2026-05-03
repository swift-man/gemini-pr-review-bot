from dataclasses import dataclass, field
from typing import Literal

# kind: 어느 GitHub endpoint 에서 왔는지 — 프롬프트 렌더링 시 그룹화 / 표시 형식 분기.
# - "review": `/pulls/{n}/reviews` (PR 전체에 대한 review submission, state 포함)
# - "issue_comment": `/issues/{n}/comments` (top-level 토론 코멘트)
# - "line_comment": `/pulls/{n}/comments` (라인 고정 review comment)
ConversationKind = Literal["review", "issue_comment", "line_comment"]


@dataclass(frozen=True)
class ConversationEntry:
    """PR 대화의 한 항목 — review submission / 토론 코멘트 / 라인 코멘트의 통합 표현.

    PR-1 (`feat/pr-conversation-context-injection`): 봇 review 직전에 fetch 해 프롬프트
    의 `=== PR CONVERSATION HISTORY ===` 섹션으로 주입. 모델이 이전 라운드의 다른 봇
    finding / 작성자 reply 를 컨텍스트로 사용해 같은 지적 반복을 회피.

    PR-2 (Layer F replies): 라인 코멘트 (kind == "line_comment") 중 본 봇이 아직
    대댓글을 안 단 것이 reply candidate. `comment_id` 로 reply 게시.
    """

    kind: ConversationKind
    author_login: str
    # GitHub `[bot]` suffix 또는 `performed_via_github_app` 존재로 판정. 사람/봇 분리는
    # 프롬프트 렌더 시 시각적 구분 + Layer F 의 reply candidate 필터링에 사용.
    is_bot: bool
    # 작성자 본인이 단 코멘트인지 — `author_login == pr.author_login`. "deferred" 응답 등
    # 작성자 의도 표현에 더 큰 weight 를 둘 때 사용.
    is_pr_author: bool
    # ISO 8601 timestamp (`2026-04-26T14:32:18Z` 등). 시간순 정렬 / cap-by-newest-first 에 사용.
    submitted_at: str
    body: str

    # review 전용
    state: str | None = None  # "APPROVED" | "COMMENTED" | "CHANGES_REQUESTED"

    # line_comment / issue_comment 식별
    comment_id: int | None = None

    # line_comment 전용 — 라인 anchor
    path: str | None = None
    line: int | None = None

    # line_comment 가 다른 코멘트의 대댓글이면 부모 id. Layer F 가 "본 봇이 이미 대댓글
    # 단 부모" 셋을 만들어 1-reply 룰을 강제하는 데 사용.
    in_reply_to_id: int | None = None


@dataclass(frozen=True)
class PrConversation:
    """PR 의 모든 대화 항목 모음 — review / issue_comment / line_comment 통합.

    `entries` 는 `submitted_at` 시간 오름차순으로 정렬돼 들어옴 (오래된 → 최신). 프롬프트
    렌더 시 newest-first cap (예산 초과 시 오래된 entry 부터 잘라냄) 을 적용 가능.

    빈 entries (PR 의 첫 review = 대화 0 건) 는 정상 케이스 — 프롬프트 섹션 자체가 빠짐
    (사용자 요구: "빈 섹션은 그대로 동작").
    """

    entries: tuple[ConversationEntry, ...] = field(default_factory=tuple)

    def is_empty(self) -> bool:
        return len(self.entries) == 0
