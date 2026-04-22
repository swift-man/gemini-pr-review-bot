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


def test_render_body_replaces_misleading_footer_when_inline_comments_dropped() -> None:
    """findings 가 비어 있고 dropped_inline_count>0 이면 솔직한 안내 문구를 찍는다.

    회귀 방지: 422 retry 경로에서 본문이 "_N건 표시_" 라고 거짓말한 채 게시되어 PR
    리뷰 수신자에게 혼란을 주던 실관측 버그(MLX#27 등) 의 재발을 막는다. 이 테스트는
    render_body 단계에서 거짓 진술이 사라지고 정확한 사실로 대체되는지를 고정한다.
    """
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.REQUEST_CHANGES,
        positives=("좋음",),
        # findings 는 비어 있음 — POST 직전에 외부에서 비웠다고 가정
    )
    body = result.render_body(dropped_inline_count=3)

    # 거짓 진술 부재
    assert "기술 단위 코멘트 3건은 각 라인에 별도 표시" not in body
    # 솔직한 사실 진술
    assert "3개 인라인 코멘트" in body
    assert "diff 범위 밖" in body
    # 본문 다른 섹션은 보존
    assert "**좋은 점**" in body
    assert "- 좋음" in body


def test_render_body_default_dropped_count_zero_does_not_add_notice() -> None:
    """기본 호출(=0) 에서는 안내 문구가 추가되지 않는다 — 하위 호환 보장."""
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
    )
    body = result.render_body()

    assert "diff 범위 밖" not in body
    assert "거부되어" not in body


def test_render_body_dropped_count_overrides_findings_footer() -> None:
    """dropped_inline_count > 0 이면 self.findings 가 있어도 안내 문구로 덮어쓴다.

    실 사용처(github_app_client retry) 에서 호출자는 같은 result 객체를 그대로 넘기지만
    "이 inline 들은 게시되지 않았다" 는 사실을 dropped_inline_count 로 알린다.
    findings 비우기 위해 dataclasses.replace 를 강제하지 않아도 동작이 정확하도록 한
    설계 결정 — 호출 단순성과 본문 정확성을 동시에 보장한다.
    """
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        findings=(Finding(path="a.py", line=5, body="[Major] x"),),
    )
    body = result.render_body(dropped_inline_count=1)

    # 거짓 footer 부재 — findings 가 있어도 안내가 우선
    assert "기술 단위 코멘트 1건은 각 라인에 별도 표시" not in body
    # 솔직한 안내가 그 자리를 차지
    assert "1개 인라인 코멘트" in body
    assert "diff 범위 밖" in body
