from dataclasses import dataclass, field

from .finding import Finding, ReviewEvent


@dataclass(frozen=True)
class ReviewResult:
    """Structured review output.

    Three sections are rendered in the top-level review body:
    - 좋은 점 (positives)
    - 개선할 점 (improvements)
    - 기술 단위 코멘트 (findings) — posted as inline, line-anchored comments

    `model` 은 리뷰를 실제로 생성한 Gemini 모델 이름. fallback 체인이 발동해 primary
    모델이 아닌 대체 모델이 결과를 돌려준 경우 본문 푸터로 운영자에게 그 사실을
    가시화한다. None 이면 푸터를 생략한다.
    """

    summary: str
    event: ReviewEvent
    positives: tuple[str, ...] = field(default_factory=tuple)
    improvements: tuple[str, ...] = field(default_factory=tuple)
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    model: str | None = None

    def render_body(self) -> str:
        parts: list[str] = [self.summary.strip()]
        if self.positives:
            parts.append("\n**좋은 점**")
            parts.extend(f"- {p}" for p in self.positives)
        if self.improvements:
            parts.append("\n**개선할 점**")
            parts.extend(f"- {i}" for i in self.improvements)
        if self.findings:
            parts.append(f"\n_기술 단위 코멘트 {len(self.findings)}건은 각 라인에 별도 표시됩니다._")
        if self.model:
            # 모든 섹션이 끝난 뒤 footer 로 렌더. 구분선(---)으로 본문과 시각적으로 분리.
            parts.append(f"\n---\n_리뷰 생성 모델: `{self.model}`_")
        return "\n".join(parts).strip()
