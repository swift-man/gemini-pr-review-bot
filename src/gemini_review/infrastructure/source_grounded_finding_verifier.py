"""모델 finding 의 quoted-text 단언이 실제 소스 라인에 존재하는지 디스크 검증.

사용자 신고 사례 5 (2026-04) 에 대한 후처리 방어:
- 모델이 `"@scope"` 같은 인용을 토큰화 단계에서 `" @scope"` 로 잘못 분해 → 거꾸로
  "원본에 공백 있음" 으로 단언하는 phantom whitespace 환각이 같은 PR 의 연속 push 에
  대해 반복 보고됨.
- 파서 단계의 패턴 강등 (`_HALLUCINATION_PATTERNS`) 은 알려진 표현만 잡지만, 이 검증은
  body 안의 backtick 인용 substring 이 실제 `path:line` 에 있는지 확인 → 신규 표현도
  잡힘. 둘은 보완 관계.
"""
import dataclasses
import logging
import re
from pathlib import Path

from gemini_review.domain import Finding, ReviewResult
from gemini_review.infrastructure.gemini_parser import _normalize_event

logger = logging.getLogger(__name__)

# 본문에 이런 키워드가 있으면 "원본 텍스트에 대한 단언" 일 가능성 높음 → quote 검증 발동.
# 정상적인 권고/제안 본문 (예: "pathlib.Path 를 쓰세요") 은 대개 이 키워드 없음 → 거짓
# 양성 줄임. 새 단언형 키워드가 관찰되면 여기에 누적.
_ASSERTION_HINTS = (
    "공백",
    "띄어쓰기",
    "오타",
    "whitespace",
    "spacing",
    "typo",
)

# body 에서 backtick 인용된 substring 추출 — 모델이 "원본은 이렇다" 단언 시 가장 자주
# 쓰는 형식. 이중/단일 따옴표는 권고에도 자주 등장해 거짓 양성이 많아 일단 backtick 만.
_BACKTICK_QUOTE = re.compile(r"`([^`]+)`")
_SEVERITY_PREFIX_HEAD = re.compile(r"^\[(Critical|Major|Minor|Suggestion)\] (.*)", re.DOTALL)
_BLOCKING_SEVERITIES = frozenset({"Critical", "Major"})


class SourceGroundedFindingVerifier:
    """체크아웃된 repo 디렉터리에서 라인을 읽어 finding 의 quote 단언을 검증."""

    def verify(self, result: ReviewResult, repo_root: Path) -> ReviewResult:
        """phantom quote 단언을 가진 [Critical]/[Major] finding 을 [Suggestion] 으로 강등.

        ### 강등 발동 조건 (모두 만족)

        1. body 가 [Critical] 또는 [Major] 시작
        2. body 에 assertion hint 키워드 ("공백", "띄어쓰기", "오타", ...) 포함 — 단언으로 추정
        3. body 에 backtick 인용 substring 이 1개 이상
        4. **인용 substring 중 단 하나도** path:line 의 raw 라인에 없음 (lenient: any-match → keep)

        ### 왜 "any-match → keep" lenient 정책인가

        codex PR #23 review #1 — 정상적인 typo/공백 finding 은 `현재 코드 → 수정안` 형태로
        두 텍스트를 함께 인용함. 예: "`usrname` 을 `username` 으로 수정". 옛 strict 정책
        ("any quote not in line → downgrade") 은 수정안 `username` 이 라인에 없다는 이유로
        정당한 finding 까지 강등시킴. 새 lenient 정책은 인용 중 **하나라도** 라인에 있으면
        통과 — `usrname` 이 라인에 있으니 정당한 finding 그대로 유지.

        Phantom case (사용자 신고): 모델이 인용한 텍스트는 `" @scope"` 단 하나, 라인에는
        `"@scope"` (공백 0). 어떤 인용도 라인에 매치 안 됨 → 강등 발동.

        강등으로 blocking 분포가 바뀌면 `_normalize_event` 가 event 를 재정합.

        ### I/O 비용

        같은 repo 안에서 여러 finding 이 같은 파일을 가리키는 경우가 흔함 (대형 PR).
        verify() 호출 1회 안에서 파일별 읽은 라인 캐시를 둬 같은 파일을 여러 번 읽지 않음
        (codex PR #23 review #2). 캐시는 호출 단위라 다음 verify() 호출 (다음 PR) 에는
        상태가 새로 시작됨 — 메모리 누수 위험 없음.
        """
        # 호출 단위 파일 라인 캐시. key=path (relative), value=lines list 또는 None (읽기 실패).
        # `None` 도 명시적으로 캐싱해 같은 누락 파일을 여러 번 읽으려 시도하지 않음.
        line_cache: dict[str, list[str] | None] = {}
        new_findings = tuple(
            self._maybe_downgrade(f, repo_root, line_cache) for f in result.findings
        )
        new_event = _normalize_event(result.event, new_findings)
        return dataclasses.replace(result, findings=new_findings, event=new_event)

    def _maybe_downgrade(
        self,
        f: Finding,
        repo_root: Path,
        line_cache: dict[str, list[str] | None],
    ) -> Finding:
        head = _SEVERITY_PREFIX_HEAD.match(f.body)
        if head is None:
            return f
        severity, rest = head.group(1), head.group(2)
        if severity not in _BLOCKING_SEVERITIES:
            return f
        # assertion hint 검사: 키워드 없으면 단순 권고/제안 — 검증 생략 (false positive 방지).
        # 정상 권고 본문 (예: "pathlib.Path 를 쓰세요") 까지 검증하면 인용된 API 이름이
        # 라인에 없다는 이유로 모두 강등돼 신호 가치를 잃는다.
        if not any(hint in f.body for hint in _ASSERTION_HINTS):
            return f
        quotes = _BACKTICK_QUOTE.findall(f.body)
        if not quotes:
            return f
        line = _read_source_line(repo_root, f.path, f.line, line_cache)
        if line is None:
            return f  # 파일 없거나 라인 범위 밖 — 검증 불가, 통과
        # Lenient: 인용 중 하나라도 라인에 매치하면 정당한 finding 으로 간주 → 통과.
        # 모든 인용이 라인에 없을 때만 phantom quote 환각으로 강등.
        # 정상 typo/공백 finding 은 `현재값` 과 `수정안` 둘 다 인용하는 패턴이 흔함 —
        # 현재값은 라인에 있으므로 자동 통과 (codex PR #23 review #1 lenient 정책).
        if any(q in line for q in quotes):
            return f
        # 모두 라인에 없음 — 어떤 인용을 진단 메시지에 노출할지 선택. 첫 번째로 충분.
        first_missing = quotes[0]
        logger.warning(
            "downgrading severity %s -> Suggestion: no backtick-quoted substring "
            "found in %s:%d (first missing: %r). assertion-hint keyword triggered "
            "verification.",
            severity,
            f.path,
            f.line,
            first_missing,
        )
        return Finding(
            path=f.path,
            line=f.line,
            body=(
                f"[Suggestion] (자동 강등: 인용 텍스트 `{first_missing}` 등이 "
                f"{f.path}:{f.line} 의 실제 라인에 없음 — phantom quote 환각 가능성, "
                f"원래 [{severity}]) {rest}"
            ),
        )


def _read_source_line(
    repo_root: Path,
    path: str,
    line: int,
    line_cache: dict[str, list[str] | None],
) -> str | None:
    """`repo_root / path` 의 1-based line 번호 라인을 반환. 없으면 None.

    같은 verify() 호출 안에서 파일별 라인 리스트를 캐싱해 같은 파일을 여러 번 읽지 않음
    (codex PR #23 review #2 — 대형 PR 의 파일별 finding 다수 시 I/O 절감).
    실패 케이스 (파일 없음·바이너리·라인 범위 초과) 는 None 반환 → 검증 생략.
    silent failure 가 finding 게시를 막으면 안 되므로 보수적으로 통과시킨다.
    """
    if path in line_cache:
        lines = line_cache[path]
    else:
        try:
            text = (repo_root / path).read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
        except OSError:
            lines = None
        line_cache[path] = lines
    if lines is None or line <= 0 or line > len(lines):
        return None
    return lines[line - 1]
