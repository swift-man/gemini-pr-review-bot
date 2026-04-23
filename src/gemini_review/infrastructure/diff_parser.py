"""Unified diff 파서 — GitHub Reviews API 가 인라인 코멘트를 받아주는 라인 집합 계산.

GitHub 의 검증 룰: `comments[].line` 이 **이 PR diff 안에 있는** 라인이어야 한다
(추가된 `+` 라인이거나 hunk 안의 context ` ` 라인). 이 룰을 어기면 422 가 나며
bulk 등록이 통째로 거부된다.

이 모듈은 `/repos/{owner}/{repo}/pulls/{n}/files` 가 돌려주는 각 파일의 `patch`
필드를 파싱해서 "RIGHT(new) 사이드 기준으로 코멘트 가능한 라인 번호 집합" 을
반환한다. 이 집합으로 finding 을 사전 필터하면 422 자체가 발생하지 않는다.
"""

import re

# unified diff hunk 헤더: `@@ -old_start,old_count +new_start,new_count @@ context...`
# 우리는 RIGHT(new) 사이드 라인 번호만 필요해서 +new_start 그룹만 캡처한다.
# `,new_count` 부분은 줄 1개일 때 생략될 수 있어 `(?:,\d+)?` 로 옵션 처리.
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def addable_lines_from_patch(patch: str | None) -> set[int]:
    """RIGHT(new) 파일 기준으로 GitHub 인라인 코멘트가 허용되는 라인 번호 집합.

    포함:
    - `+` 로 시작하는 추가 라인 (단, `+++` 헤더는 제외)
    - 한 hunk 안의 context 라인 (` ` 로 시작) — GitHub 도 hunk 범위 안의 변경 안 된
      라인엔 코멘트 허용

    제외:
    - `-` 로 시작하는 제거 라인 (RIGHT 사이드에는 존재하지 않음)
    - hunk 바깥 (전혀 변경 안 된 영역)

    `patch` 가 None 이거나 빈 문자열이면 빈 집합 반환 — binary 파일·삭제 파일·
    GitHub 가 truncate 한 거대 파일 등에서 일어남.
    """
    if not patch:
        return set()

    addable: set[int] = set()
    new_line_no = 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            match = _HUNK_HEADER.match(line)
            if match:
                # hunk 시작 직전 위치로 세팅. 첫 번째 +/space 라인을 만나면 +1 되어
                # 정확한 시작 라인 번호가 된다.
                new_line_no = int(match.group(1)) - 1
        elif line.startswith("+++") or line.startswith("---"):
            # diff 헤더 라인 (`+++ b/path`, `--- a/path`) — 본문 아님
            continue
        elif line.startswith("+"):
            new_line_no += 1
            addable.add(new_line_no)
        elif line.startswith(" "):
            # context 라인 — 변경되지 않았지만 hunk 안에 있으니 코멘트 허용
            new_line_no += 1
            addable.add(new_line_no)
        elif line.startswith("-"):
            # RIGHT 사이드에 없는 라인 — 카운터를 옮기지 않는다
            continue
        else:
            # `\ No newline at end of file` 같은 메타 라인 — 무시
            continue
    return addable
