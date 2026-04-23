from dataclasses import dataclass, field

from .finding import Finding, ReviewEvent


@dataclass(frozen=True)
class ReviewResult:
    """구조화된 리뷰 출력물.

    리뷰 본문 상단에는 세 섹션이 렌더된다:
    - 좋은 점 (positives)
    - 개선할 점 (improvements)
    - 기술 단위 코멘트 (findings) — 라인에 고정된 인라인 코멘트로 게시

    `model` 은 이 리뷰를 생성한 모델 식별자. 설정돼 있으면 본문 푸터로 렌더되고
    None 이면 생략된다. 값의 의미와 어떤 상황에서 채워지는지는 채워 주는 엔진
    구현의 책임 (예: `GeminiCliEngine` 이 fallback 이후 실제 성공한 모델명을 주입).
    """

    summary: str
    event: ReviewEvent
    positives: tuple[str, ...] = field(default_factory=tuple)
    improvements: tuple[str, ...] = field(default_factory=tuple)
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    model: str | None = None

    def render_body(self, *, dropped_inline_count: int = 0) -> str:
        """리뷰 본문 마크다운을 렌더.

        `dropped_inline_count` 는 GitHub Reviews API 가 422 로 거부해서 본문만 게시되는
        재시도 경로에서 호출자가 주입한다. 이 값이 양수면 `self.findings` 가 비어 있든
        말든 **항상** 솔직한 안내 ("N개 인라인 코멘트가 거부됨") 로 footer 를 대체한다.
        호출자가 같은 result 객체를 그대로 넘기더라도 본문이 거짓이 되지 않도록 하기
        위함 — 리뷰 수신자가 "N건 표시" footer 만 보고 인라인을 찾으러 가는 헛수고를 막음.
        """
        parts: list[str] = [self.summary.strip()]
        if self.positives:
            parts.append("\n**좋은 점**")
            parts.extend(f"- {p}" for p in self.positives)
        if self.improvements:
            parts.append("\n**개선할 점**")
            parts.extend(f"- {i}" for i in self.improvements)

        if dropped_inline_count > 0:
            # 재시도 경로 우선 — caller 가 "이 inline 들은 게시되지 않았다" 라고 알려줬으니
            # findings 의 존재 여부와 무관하게 솔직한 사실로 덮는다.
            parts.append(
                f"\n_(주: 모델이 제시한 {dropped_inline_count}개 인라인 코멘트가 PR diff "
                "범위 밖이라 GitHub 검증에 거부되어 본문만 게시됐습니다.)_"
            )
        elif self.findings:
            parts.append(
                f"\n_기술 단위 코멘트 {len(self.findings)}건은 각 라인에 별도 표시됩니다._"
            )

        if self.model:
            # 모든 섹션이 끝난 뒤 footer 로 렌더. 구분선(---)으로 본문과 시각적으로 분리.
            parts.append(f"\n---\n_리뷰 생성 모델: `{self.model}`_")
        return "\n".join(parts).strip()
