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


def test_verify_downgrades_when_file_missing_and_assertion_hint_present(
    tmp_path: Path,
) -> None:
    """디스크에 파일 없는 상태로 assertion-hint + 인용된 [Critical] 이 들어오면 강등.

    회귀 방지 (codex PR #23 review #3): 이전엔 silent pass 였음. 모델이 본 적 없는 파일
    (체크아웃에 없는 삭제 파일 / fictional path) 에 대한 phantom quote `[Critical]` 이
    그대로 차단 신호로 게시되던 보안+정확성 회귀. 이제 검증 불가 = 강등.
    """
    finding = Finding(
        path="missing.py", line=1, body="[Critical] 공백이 `\" @x\"` 에 있음"
    )

    out = SourceGroundedFindingVerifier().verify(_result(finding), tmp_path)

    assert out.findings[0].body.startswith("[Suggestion]"), (
        "파일 없음 + assertion-hint + 인용 → 강등돼야 (모델이 본 파일 아님)"
    )
    assert "missing" in out.findings[0].body  # status 가 메시지에 노출
    assert "원래 [Critical]" in out.findings[0].body


def test_verify_does_not_downgrade_missing_file_finding_without_assertion_hint(
    tmp_path: Path,
) -> None:
    """파일 없어도 assertion-hint 없으면 강등 안 함 — 일반 finding 보호.

    회귀 방지: assertion-hint 없는 본문은 일반 권고/제안일 가능성. 파일이 잠시 못 읽혀도
    그 자체로 phantom quote 신호는 아님 → 보존. 검증 발동 조건은 hint+quote 모두 있을 때만.
    """
    finding = Finding(
        path="missing.py",
        line=1,
        body="[Critical] 이 모듈 전체 구조가 다른 모듈과 결합도가 너무 높습니다.",
    )

    out = SourceGroundedFindingVerifier().verify(_result(finding), tmp_path)

    assert out.findings[0].body.startswith("[Critical]")


def test_verify_downgrades_when_line_out_of_range_and_assertion_hint_present(
    tmp_path: Path,
) -> None:
    """파일은 있지만 line 이 파일 길이 밖이면 phantom — 강등.

    회귀 방지 (codex PR #23 review #3): 모델이 잘못된 라인을 가리키는 환각.
    파일은 보지만 라인 번호가 맞지 않는 경우 "어떤 라인" 인지 알 수 없으니 검증 불가.
    """
    _write(tmp_path, "x.py", "x = 1\n")
    finding = Finding(
        path="x.py", line=999, body="[Critical] 공백이 `\" @x\"` 에 있음"
    )

    out = SourceGroundedFindingVerifier().verify(_result(finding), tmp_path)

    assert out.findings[0].body.startswith("[Suggestion]")
    assert "out_of_range" in out.findings[0].body


# --- Path traversal 방어 (gemini PR #23 review) ----------------------------


def test_verify_downgrades_on_path_traversal_attempt(
    tmp_path: Path, caplog: object
) -> None:
    """모델 출력의 path 가 repo_root 밖을 가리키면 거부 + 강등.

    회귀 방지 (gemini PR #23 review): `path` 는 모델 출력에서 온 신뢰 불가 입력. `..`
    같은 시퀀스로 repo_root 밖 (예: `/etc/passwd`) 을 가리킬 수 있음. 디스크에서 실제로
    임의 파일을 읽는 건 정보 노출 경로가 됨. resolve() 후 repo_root 의 자식인지 확인.
    """
    import logging
    import pytest as _pytest
    caplog_typed: _pytest.LogCaptureFixture = caplog  # type: ignore[assignment]

    finding = Finding(
        path="../../../etc/passwd",
        line=1,
        body="[Critical] 공백이 `\" :root:\"` 에 있음",
    )

    with caplog_typed.at_level(logging.WARNING):
        out = SourceGroundedFindingVerifier().verify(_result(finding), tmp_path)

    assert out.findings[0].body.startswith("[Suggestion]"), "traversal path 는 강등"
    assert "traversal" in out.findings[0].body
    # 운영 관측: traversal 시도가 WARN 로 기록돼야 모니터링 가능
    traversal_warns = [
        r for r in caplog_typed.records if "path traversal" in r.getMessage()
    ]
    assert len(traversal_warns) == 1


# --- Case-insensitive English assertion hints (codex PR #23 review #2) -----


def test_verify_matches_uppercase_english_assertion_hints(tmp_path: Path) -> None:
    """`Whitespace`/`Typo` 처럼 첫글자 대문자 영문 키워드도 검증 발동해야 한다.

    회귀 방지 (codex PR #23 review #2): 모델은 자연어 출력에서 종종 문장 첫 단어를
    대문자로 시작 ("Whitespace issue here..."). 이전엔 case-sensitive 매칭이라 hint
    감지를 못 해 검증을 생략했음 — 환각 finding 이 그대로 통과.
    """
    _write(tmp_path, "x.py", "\n" * 4 + "x = 1\n")
    finding = Finding(
        path="x.py",
        line=5,
        body="[Critical] Whitespace issue: `\" leading\"` should not be there.",
    )

    out = SourceGroundedFindingVerifier().verify(_result(finding), tmp_path)

    # `" leading"` 은 라인에 없음 + 대문자 hint 감지 → 강등
    assert out.findings[0].body.startswith("[Suggestion]"), (
        "Whitespace (대문자) 도 hint 매칭 → 강등 발동해야"
    )


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


# --- Lenient any-match policy (codex PR #23 review #1 회귀 방지) -----------


def test_verify_lenient_keeps_finding_when_typo_quote_matches_even_if_fix_does_not(
    tmp_path: Path,
) -> None:
    """정상 typo finding 은 `현재값` `수정안` 두 텍스트를 함께 인용하는 패턴이 흔함.

    회귀 방지 (codex PR #23 review #1): 옛 strict 정책 ("any quote NOT in line → 강등")
    은 수정안이 라인에 없다는 이유로 정당한 typo finding 까지 강등시킴. 새 lenient
    정책은 인용 중 **하나라도** 라인에 있으면 통과 — 현재값이 라인에 있으니 보존.

    실제 typo `usrname` 이 라인에 있고, 모델이 "`usrname` 을 `username` 으로 수정"
    이라고 적으면: `username` 은 라인에 없지만 `usrname` 은 있음 → 통과해야 한다.
    """
    _write(tmp_path, "x.py", "\n" * 4 + 'def hello(usrname):\n')
    finding = Finding(
        path="x.py",
        line=5,
        body="[Critical] 변수명에 오타가 있습니다. `usrname` 을 `username` 으로 수정하세요.",
    )

    out = SourceGroundedFindingVerifier().verify(_result(finding), tmp_path)

    assert out.findings[0].body.startswith("[Critical]"), (
        "현재값 `usrname` 이 라인에 있으므로 정당한 finding 으로 통과해야 — lenient any-match 정책"
    )
    assert "자동 강등" not in out.findings[0].body


def test_verify_lenient_downgrades_when_no_quote_matches_line(tmp_path: Path) -> None:
    """lenient 정책 검증의 negative side: 인용 모두 라인에 없으면 강등 발동.

    회귀 방지: lenient 가 너무 관대해져서 phantom case 도 통과시키면 PR #23 의 본 목적을
    잃는다. 모든 인용이 라인에 없을 때만 강등 발동해야 한다.
    """
    _write(tmp_path, "x.py", "\n" * 4 + 'def hello(name):\n')
    finding = Finding(
        path="x.py",
        line=5,
        body="[Critical] 공백 오타 — `\" name\"` 이 `\"name\"` 으로 돼 있어야 합니다.",
    )

    out = SourceGroundedFindingVerifier().verify(_result(finding), tmp_path)

    assert out.findings[0].body.startswith("[Suggestion]"), (
        "인용 둘 (`\" name\"`, `\"name\"`) 모두 라인에 없으면 phantom → 강등"
    )


# --- 파일별 라인 캐시 (codex PR #23 review #2 회귀 방지) -------------------


def test_verify_uses_per_call_file_cache_to_avoid_repeated_io(
    tmp_path: Path,
) -> None:
    """같은 파일을 가리키는 finding 여러 개에 대해 디스크 read 가 1회로 줄어야 한다.

    회귀 방지 (codex PR #23 review #2): 캐시 없이 매 finding 마다 `read_text()` 호출하면
    대형 PR 의 같은 파일에 finding 이 N 개 있을 때 N 번 디스크 read — 큰 PR 일수록 비례
    낭비. 호출 단위 캐시로 N → 1 로 줄어야.
    """
    import pathlib

    import pytest

    _write(tmp_path, "x.py", "\n" * 9 + 'def f(x):\n')
    findings = tuple(
        Finding(
            path="x.py",
            line=10,
            body=f"[Major] 공백 오타 #{i} `\" not_real\"`",
        )
        for i in range(5)
    )

    read_count = 0
    original = pathlib.Path.read_text

    def counting_read(self: pathlib.Path, *args: object, **kwargs: object) -> str:
        nonlocal read_count
        read_count += 1
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(pathlib.Path, "read_text", counting_read)
        SourceGroundedFindingVerifier().verify(_result(*findings), tmp_path)
    finally:
        mp.undo()

    assert read_count == 1, (
        f"같은 파일 5 개 finding → read_text 1번이어야 (캐시 작동). 실제 {read_count}번"
    )


def test_verify_caches_missing_files_to_avoid_repeated_failed_io(
    tmp_path: Path,
) -> None:
    """없는 파일을 가리키는 finding 여러 개도 read 시도가 1번이어야 한다.

    회귀 방지: 캐시가 None (읽기 실패) 도 명시적으로 저장해야 동일 누락 파일을 여러 번
    open() 시도하지 않는다. file system stat call 도 비용.
    """
    import pathlib

    import pytest

    findings = tuple(
        Finding(
            path="missing.py",
            line=1,
            body=f"[Major] 공백 오타 #{i} `\" not_real\"`",
        )
        for i in range(3)
    )

    read_count = 0
    original = pathlib.Path.read_text

    def counting_read(self: pathlib.Path, *args: object, **kwargs: object) -> str:
        nonlocal read_count
        read_count += 1
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(pathlib.Path, "read_text", counting_read)
        SourceGroundedFindingVerifier().verify(_result(*findings), tmp_path)
    finally:
        mp.undo()

    assert read_count == 1, (
        f"누락 파일 3 finding → read_text 1번 시도여야 (None 도 캐싱). 실제 {read_count}번"
    )
