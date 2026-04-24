"""SourceGroundedFindingVerifier 단위 테스트.

핵심: phantom quote 환각 — 모델이 backtick 으로 인용한 텍스트가 실제 path:line 에
없으면 [Critical]/[Major] finding 을 [Suggestion] 으로 강등.

검증 발동 조건이 모두 만족돼야 강등:
1. body 가 [Critical] 또는 [Major] 시작
2. body 에 assertion-hint 키워드 ("공백", "띄어쓰기", "오타", ...) 포함
3. body 에 backtick 인용 substring 존재
4. 인용 substring 중 하나라도 path:line 의 라인에 없음
"""
from pathlib import Path

from gemini_review.domain import Finding, ReviewEvent, ReviewResult
from gemini_review.infrastructure.source_grounded_finding_verifier import (
    SourceGroundedFindingVerifier,
)


def _result(*findings: Finding, event: ReviewEvent = ReviewEvent.REQUEST_CHANGES) -> ReviewResult:
    return ReviewResult(summary="x", event=event, findings=findings)


def _write(repo: Path, path: str, content: str) -> None:
    full = repo / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


# --- phantom quote 강등 ------------------------------------------------------


def test_verify_downgrades_critical_when_quoted_text_not_in_actual_line(
    tmp_path: Path,
) -> None:
    """실관측 회귀 (사용자 신고 사례 5): 모델이 `"@scope"` 인용을 `" @scope"` 로 잘못
    토큰화 → "원본에 공백" 단언. 실제 라인엔 공백 0 → 강등 발동.
    """
    _write(tmp_path, "README.md", '\n' * 119 + 'import "@swift-man/material-design-color"\n')
    finding = Finding(
        path="README.md",
        line=120,
        body="[Major] 패키지명 앞에 불필요한 공백(`\" @swift-man/material-design-color\"`)이 있습니다.",
    )

    out = SourceGroundedFindingVerifier().verify(_result(finding), tmp_path)

    assert out.findings[0].body.startswith("[Suggestion]"), "phantom quote → 강등돼야"
    assert "원래 [Major]" in out.findings[0].body, "원래 등급 보존 (silent rewrite 방지)"
    assert "phantom quote" in out.findings[0].body
    # blocking 0 → REQUEST_CHANGES 약화
    assert out.event == ReviewEvent.COMMENT


def test_verify_does_not_downgrade_when_quote_actually_exists_in_line(
    tmp_path: Path,
) -> None:
    """인용된 텍스트가 실제로 라인에 있으면 강등 안 함 — 정당한 단언은 보존.

    회귀 방지: 검증이 너무 공격적이면 진짜 공백 버그 지적도 강등돼 신호 가치 하락.
    """
    # 실제 라인에 phantom 공백이 있는 경우 (모델 단언이 사실)
    _write(tmp_path, "x.py", "\n" * 4 + 'CONST = " @bug"  # 의도치 않은 선행 공백\n')
    finding = Finding(
        path="x.py",
        line=5,
        body="[Critical] 문자열에 불필요한 공백이 있습니다 (`\" @bug\"`).",
    )

    out = SourceGroundedFindingVerifier().verify(_result(finding), tmp_path)

    assert out.findings[0].body.startswith("[Critical]"), "정당한 단언은 강등 안 함"


def test_verify_skips_non_assertion_findings(tmp_path: Path) -> None:
    """assertion-hint 키워드 없는 finding 은 검증 생략 — 정상 권고/제안 보호.

    회귀 방지: "pathlib.Path 를 쓰세요" 같은 권고는 인용된 API 이름이 라인에 없는 게
    당연 (그래서 권고함). 이런 정상 본문까지 검증하면 모두 강등돼 무용지물.
    """
    _write(tmp_path, "x.py", "\n" * 9 + "import os\n")
    finding = Finding(
        path="x.py",
        line=10,
        body="[Critical] `pathlib.Path` 를 쓰면 더 안전합니다.",  # API 권고, 단언 아님
    )

    out = SourceGroundedFindingVerifier().verify(_result(finding), tmp_path)

    assert out.findings[0].body.startswith("[Critical]"), "권고 본문은 강등 안 됨"


def test_verify_skips_minor_and_suggestion_findings(tmp_path: Path) -> None:
    """이미 [Minor]/[Suggestion] 인 finding 은 검증 생략 — 강등할 의미 없음."""
    _write(tmp_path, "x.py", "\n" * 4 + "x = 1\n")
    findings = (
        Finding(path="x.py", line=5, body="[Minor] 이상한 띄어쓰기 오타가 `foo bar` 같이 있음"),
        Finding(path="x.py", line=5, body="[Suggestion] 이상한 공백 (`\"  \"`) 도 마찬가지"),
    )

    out = SourceGroundedFindingVerifier().verify(_result(*findings), tmp_path)

    assert out.findings[0].body.startswith("[Minor]")
    assert out.findings[1].body.startswith("[Suggestion]")


def test_verify_skips_findings_without_backtick_quotes(tmp_path: Path) -> None:
    """assertion-hint 만 있고 인용 없으면 검증할 대상 없어 통과.

    빈약한 단언 본문 (예: "공백 있음") 은 검증 불가 — 그대로 두고 모델 책임에 맡김.
    """
    _write(tmp_path, "x.py", "\n" * 4 + "x = 1\n")
    finding = Finding(
        path="x.py", line=5, body="[Critical] 이 라인에 불필요한 공백이 있습니다."
    )

    out = SourceGroundedFindingVerifier().verify(_result(finding), tmp_path)

    assert out.findings[0].body.startswith("[Critical]"), "인용 없으면 검증 불가 → 강등 안 함"


def test_verify_skips_when_file_missing(tmp_path: Path) -> None:
    """디스크에 파일 없으면 검증 생략 (강등 안 함) — silent failure 가 finding 게시를 막으면 안 됨."""
    finding = Finding(
        path="missing.py", line=1, body="[Critical] 공백이 `\" @x\"` 에 있음"
    )

    out = SourceGroundedFindingVerifier().verify(_result(finding), tmp_path)

    assert out.findings[0].body.startswith("[Critical]"), "파일 없음 → 검증 생략, 통과"


def test_verify_skips_when_line_out_of_range(tmp_path: Path) -> None:
    """파일은 있지만 line 이 파일 길이 밖이면 검증 생략."""
    _write(tmp_path, "x.py", "x = 1\n")
    finding = Finding(
        path="x.py", line=999, body="[Critical] 공백이 `\" @x\"` 에 있음"
    )

    out = SourceGroundedFindingVerifier().verify(_result(finding), tmp_path)

    assert out.findings[0].body.startswith("[Critical]")


# --- event 재정합 -----------------------------------------------------------


def test_verify_re_normalizes_event_when_only_blocking_dropped(tmp_path: Path) -> None:
    """모든 blocking finding 이 강등되면 REQUEST_CHANGES → COMMENT 약화."""
    _write(tmp_path, "x.py", "\n" * 4 + "real_content\n")
    findings = (
        Finding(
            path="x.py",
            line=5,
            body="[Major] phantom 공백 단언 `\" not_real\"`",
        ),
    )

    out = SourceGroundedFindingVerifier().verify(
        _result(*findings, event=ReviewEvent.REQUEST_CHANGES), tmp_path
    )

    assert out.findings[0].body.startswith("[Suggestion]")
    assert out.event == ReviewEvent.COMMENT, "강등으로 blocking 0 → REQUEST_CHANGES 약화"


def test_verify_keeps_request_changes_when_other_blocking_survives(tmp_path: Path) -> None:
    """일부만 강등되고 다른 [Critical] 이 살아있으면 event 유지."""
    _write(tmp_path, "x.py", "\n" * 4 + "real_content\n")
    findings = (
        Finding(
            path="x.py",
            line=5,
            body="[Major] phantom 공백 단언 `\" not_real\"`",
        ),
        Finding(
            path="x.py",
            line=5,
            body="[Critical] 진짜 차단 사유 (단언 키워드 없음 → 검증 생략)",
        ),
    )

    out = SourceGroundedFindingVerifier().verify(
        _result(*findings, event=ReviewEvent.REQUEST_CHANGES), tmp_path
    )

    assert out.findings[0].body.startswith("[Suggestion]")  # phantom 강등
    assert out.findings[1].body.startswith("[Critical]")  # 정당한 finding 유지
    assert out.event == ReviewEvent.REQUEST_CHANGES, "blocking 살아있으면 약화 안 함"
