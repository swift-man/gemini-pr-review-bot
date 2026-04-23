from gemini_review.domain import Finding, ReviewEvent, ReviewResult


def test_render_body_includes_three_sections() -> None:
    result = ReviewResult(
        summary="요약입니다.",
        event=ReviewEvent.COMMENT,
        positives=("Protocol 기반 DIP",),
        improvements=("계층 경계 강화",),
        findings=(Finding(path="a.py", line=1, body="functools.cache를 고려하세요."),),
    )
    body = result.render_body()
    assert body.startswith("요약입니다.")
    assert "**좋은 점**" in body
    assert "- Protocol 기반 DIP" in body
    assert "**개선할 점**" in body
    assert "- 계층 경계 강화" in body
    assert "기술 단위 코멘트 1건" in body


def test_render_body_omits_empty_sections() -> None:
    result = ReviewResult(summary="요약", event=ReviewEvent.COMMENT)
    body = result.render_body()
    assert body == "요약"


def test_render_body_without_findings_does_not_mention_inline_comments() -> None:
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        positives=("좋음",),
    )
    body = result.render_body()
    assert "기술 단위 코멘트" not in body


def test_render_body_appends_model_footer_when_model_is_set() -> None:
    """모델명이 설정돼 있으면 본문 마지막에 구분선과 함께 푸터로 렌더.

    fallback 체인 발동 시 어떤 모델이 실제로 리뷰를 만들었는지 PR 본문에서
    바로 알 수 있어야 한다.
    """
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        positives=("좋음",),
        model="gemini-2.5-pro",
    )
    body = result.render_body()

    assert body.endswith("_리뷰 생성 모델: `gemini-2.5-pro`_")
    # 본문과 시각적으로 분리되는 구분선이 있어야 푸터로 읽힌다.
    assert "---" in body
    # 모델 푸터가 기존 섹션 뒤에 와야 한다 (중간에 끼어들면 안 됨).
    assert body.index("**좋은 점**") < body.index("gemini-2.5-pro")


def test_render_body_omits_model_footer_when_model_is_none() -> None:
    """모델명이 없으면(기본값) 푸터를 찍지 않고 기존 동작을 유지한다 — 하위 호환 보장."""
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
    )
    body = result.render_body()

    assert "리뷰 생성 모델" not in body
    assert "---" not in body
    assert body == "요약"


def test_render_body_surfaces_dropped_findings_with_path_line_and_body() -> None:
    """surface_findings 에 들어온 finding 들이 본문에 file:line + 등급·내용으로 노출된다.

    핵심 회귀 방지: PR diff 범위 밖이라 인라인으로 못 붙는 finding 의 정보가 사라지면,
    수신자는 모델이 본 문제를 알 수 없게 된다. body 가 이미 `[등급]` 접두사를 갖고
    있으므로(PR #13) 추가 가공 없이 그대로 노출되는지 확인.
    """
    surfaced = (
        Finding(path="src/auth.py", line=42, body="[Critical] sys.exit(1) 호출이 ..."),
        Finding(path="src/utils.py", line=101, body="[Major] race condition ..."),
    )
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.REQUEST_CHANGES,
        positives=("좋음",),
        # findings 는 인라인 가능한 것만 — surface 는 별도 인자로 들어옴
    )
    body = result.render_body(surface_findings=surfaced)

    # 본문에 path:line + body 모두 있어야 한다
    assert "src/auth.py:42" in body
    assert "[Critical] sys.exit(1) 호출이" in body
    assert "src/utils.py:101" in body
    assert "[Major] race condition" in body
    # 안내 문구
    assert "2개 코멘트는 PR diff 범위 밖" in body
    # 헤더
    assert "**드롭된 라인 지적**" in body


def test_render_body_inline_count_excludes_surfaced() -> None:
    """findings=5 + surface=2 인 경우, 인라인 안내는 (5-2)=3 으로 정확히 표시.

    회귀 방지: render_body 가 self.findings 길이를 그대로 인라인 카운트로 쓰면
    "5건 인라인 표시" 라고 찍히지만 실제 게시는 3건만이라 다시 거짓 진술이 됨.
    """
    findings = tuple(
        Finding(path=f"f{i}.py", line=i, body=f"[Minor] {i}") for i in range(1, 6)
    )
    surfaced = findings[3:5]  # 마지막 2개가 surface
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        findings=findings,
    )
    body = result.render_body(surface_findings=surfaced)

    # 인라인 안내는 5 - 2 = 3 건
    assert "기술 단위 코멘트 3건은 각 라인에 별도 표시" in body
    # 거짓 5건이 들어가지 않는다
    assert "기술 단위 코멘트 5건" not in body
    # surface 카운트도 별도로 표시
    assert "2개 코멘트는 PR diff 범위 밖" in body


def test_render_body_default_no_surface_no_drop_notice() -> None:
    """기본 호출(surface_findings=()) 에서는 드롭 관련 문구가 일절 추가되지 않음."""
    result = ReviewResult(summary="요약", event=ReviewEvent.COMMENT)
    body = result.render_body()
    assert "diff 범위 밖" not in body
    assert "드롭된 라인 지적" not in body


def test_render_body_all_inline_no_surface_uses_only_inline_count_footer() -> None:
    """findings 가 모두 인라인 가능 (surface 비어 있음) 인 경우, 기존 footer 만 표시."""
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        findings=(
            Finding(path="a.py", line=1, body="[Minor] x"),
            Finding(path="b.py", line=2, body="[Minor] y"),
        ),
    )
    body = result.render_body(surface_findings=())
    assert "기술 단위 코멘트 2건은 각 라인에 별도 표시" in body
    assert "드롭된 라인 지적" not in body
