from typing import Protocol

from gemini_review.domain import FileDump, PrConversation, PullRequest, ReviewResult


class ReviewEngine(Protocol):
    """프롬프트를 LLM 에 태워 구조화된 리뷰 결과로 되돌려주는 추상화.

    기본 구현은 `GeminiCliEngine` (Gemini CLI + Google OAuth) 이지만, 동일한
    Protocol 을 만족하면 다른 모델(로컬 MLX, Codex 등) 로 교체 가능합니다 (OCP).
    """

    def review(
        self,
        pr: PullRequest,
        dump: FileDump,
        conversation: PrConversation | None = None,
    ) -> ReviewResult:
        """전체 코드베이스 덤프 + (선택) PR 대화 컨텍스트 → 한국어 리뷰 결과.

        `conversation` 이 None / empty 면 기존 동작 그대로 (PR-1 추가, backward-compat).
        주어지면 프롬프트의 `=== PR CONVERSATION HISTORY ===` 섹션으로 주입돼 모델이
        다른 reviewer 의견 / 작성자 reply 를 컨텍스트로 사용 — 같은 지적 반복 회피.
        """
        ...

    def review_diff(
        self,
        pr: PullRequest,
        diff_text: str,
        conversation: PrConversation | None = None,
    ) -> ReviewResult:
        """전체 코드베이스가 컨텍스트 한도를 초과할 때의 fallback — diff 만 입력으로 리뷰.

        `review` 와 동일한 JSON 스키마와 환각 방어 정책을 따르되, 입력은 변경 파일의
        unified diff 만 (RIGHT-line annotated). 모델은 cross-file 단언이 불가하다는
        제약을 프롬프트로 강하게 안내받고 [Critical]/[Major] 발행은 diff 만으로 명확히
        검증 가능한 경우에만 허용된다.

        `conversation` 의 의미는 `review()` 와 동일 — None/empty 면 섹션 생략 (기존 흐름).
        """
        ...
