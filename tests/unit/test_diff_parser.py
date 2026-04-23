"""diff_parser.addable_lines_from_patch 의 엣지 케이스 고정.

이 함수는 GitHub Reviews API 의 "comments[].line 은 diff 안 라인이어야 한다" 룰을
서버 호출 없이 재현한다 — 잘못 계산되면 422 가 다시 출현하므로 회귀 방지가 중요.
"""

from gemini_review.infrastructure.diff_parser import addable_lines_from_patch


def test_added_lines_only() -> None:
    """순수 추가 hunk — 추가된 라인만 RIGHT 사이드에 들어간다."""
    patch = (
        "@@ -10,0 +11,3 @@\n"
        "+added line 11\n"
        "+added line 12\n"
        "+added line 13\n"
    )
    assert addable_lines_from_patch(patch) == {11, 12, 13}


def test_added_lines_with_context() -> None:
    """추가 + context 혼합 — context 라인도 인라인 코멘트 가능 (GitHub 룰)."""
    patch = (
        "@@ -10,3 +10,4 @@\n"
        " context 10\n"
        " context 11\n"
        "+added 12\n"
        " context 13\n"
    )
    assert addable_lines_from_patch(patch) == {10, 11, 12, 13}


def test_removed_lines_do_not_advance_new_counter() -> None:
    """제거(`-`) 라인은 RIGHT 사이드에 없어 카운터를 옮기지 않는다.

    회귀 방지: 만약 `-` 라인에서 new_line_no 를 증가시키면 이후 모든 추가 라인의
    인덱스가 1씩 밀려 422 가 발생한다.
    """
    patch = (
        "@@ -10,4 +10,3 @@\n"
        " context 10\n"
        "-removed (was line 11 in old)\n"
        "+added 11 in new\n"
        " context 12\n"
    )
    # 정답: {10, 11, 12} — 제거된 라인이 카운터를 안 옮김
    assert addable_lines_from_patch(patch) == {10, 11, 12}


def test_multiple_hunks() -> None:
    """여러 hunk 가 한 patch 에 있는 경우 — 각 hunk 의 헤더로 카운터가 리셋된다."""
    patch = (
        "@@ -10,2 +10,3 @@\n"
        " ctx\n"
        "+a\n"
        " ctx\n"
        "@@ -100,1 +101,2 @@\n"
        " ctx\n"
        "+b\n"
    )
    assert addable_lines_from_patch(patch) == {10, 11, 12, 101, 102}


def test_diff_file_headers_are_ignored() -> None:
    """`+++ b/path` / `--- a/path` 헤더는 추가 라인이 아니다."""
    patch = (
        "--- a/src/x.py\n"
        "+++ b/src/x.py\n"
        "@@ -1,1 +1,2 @@\n"
        " ctx\n"
        "+real add\n"
    )
    assert addable_lines_from_patch(patch) == {1, 2}


def test_no_newline_marker_is_ignored() -> None:
    """`\\ No newline at end of file` 메타 라인은 카운터에 영향 없음."""
    patch = (
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
        "\\ No newline at end of file\n"
    )
    assert addable_lines_from_patch(patch) == {1}


def test_empty_or_none_patch_returns_empty_set() -> None:
    """binary 파일 / 삭제 / GitHub truncate 케이스 — patch 가 비거나 None."""
    assert addable_lines_from_patch(None) == set()
    assert addable_lines_from_patch("") == set()


def test_single_line_addition_without_count_in_header() -> None:
    """hunk 헤더의 count 가 생략 가능: `@@ -10 +10 @@` (1줄일 때)."""
    patch = (
        "@@ -10 +10 @@\n"
        "-old\n"
        "+new\n"
    )
    assert addable_lines_from_patch(patch) == {10}


def test_large_hunk_starting_at_high_line() -> None:
    """파일 후반부의 hunk — 카운터가 정확히 새 시작 위치에서 출발."""
    patch = (
        "@@ -1000,3 +1500,3 @@\n"
        " a\n"
        "-b\n"
        "+B\n"
        " c\n"
    )
    assert addable_lines_from_patch(patch) == {1500, 1501, 1502}
