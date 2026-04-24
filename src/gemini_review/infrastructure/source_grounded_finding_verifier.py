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
# 영문 키워드는 모두 소문자로 보관하고, 매칭 시 body 도 소문자로 변환해 비교 — 모델이
# `Whitespace`/`Typo` 처럼 첫 글자 대문자로 시작해도 검증이 발동하도록
# (codex PR #23 review #2). 한글 키워드는 case 무관.
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

# 본문이 "현재값 → 수정안" 형태의 정상 typo/공백 finding 임을 시사하는 표지.
# 이 표지가 있으면 lenient 매칭 (인용 중 하나라도 라인에 있으면 통과 — 현재값만 검증);
# 없으면 strict 매칭 (모든 인용이 라인에 있어야 통과 — phantom + real 혼합 차단).
# 영문 패턴은 소문자로 보관, 매칭 시 body 도 소문자화 (codex PR #23 review #4 정책 조정).
_FIX_PATTERN_HINTS = (
    "→",  # 한국어 화살표
    "->",  # ASCII 화살표
    "로 변경",
    "로 수정",
    "으로 변경",
    "으로 수정",
    "대신",  # "X 대신 Y 사용"
    "should be",
    "instead of",
    "rather than",
    "replace",
)


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
        # body 를 소문자로 변환해 매칭 — 영문 키워드의 첫글자 대문자 케이스 (예: `Whitespace`,
        # `Typo`) 도 잡힘 (codex PR #23 review #2).
        body_lower = f.body.lower()
        if not any(hint in body_lower for hint in _ASSERTION_HINTS):
            return f
        quotes = _BACKTICK_QUOTE.findall(f.body)
        if not quotes:
            return f
        line, status = _read_source_line(repo_root, f.path, f.line, line_cache)
        # 디스크에서 라인을 못 읽은 케이스의 처리 (codex PR #23 review #3):
        #   - "missing": 변경 파일 목록에는 있지만 체크아웃엔 없음 (삭제된 파일 등)
        #     → 모델이 실제로 본 파일이 아님 → phantom quote 가능성 → 강등
        #   - "out_of_range": 파일은 있지만 line 이 범위 밖
        #     → 모델이 잘못된 라인을 가리킨 환각 → 강등
        #   - "traversal": path traversal 시도 (../../etc/passwd 류)
        #     → 명백히 신뢰 불가 입력 → 강등
        # 이전엔 이 모든 케이스가 silent pass 였음. 모델이 못 본 / 잘못 본 / 악의적 path 의
        # phantom finding 이 [Critical] 그대로 게시됐던 보안+정확성 회귀.
        if line is None:
            self._log_unverifiable_downgrade(severity, f, status)
            return Finding(
                path=f.path,
                line=f.line,
                body=(
                    f"[Suggestion] (자동 강등: {f.path}:{f.line} 의 실제 라인을 "
                    f"검증할 수 없음 [{status}] — 모델이 본 파일이 아닐 가능성, "
                    f"원래 [{severity}]) {rest}"
                ),
            )
        # 매칭 정책 (codex PR #23 review #1 → #4 의 추가 조정):
        #
        # - **fix-pattern lenient**: body 가 "현재값 → 수정안" 형태의 표지 (`→`, `로 변경`,
        #   `should be` 등) 를 포함하면 정상 typo finding 으로 보고 lenient 매칭 — 인용 중
        #   하나라도 라인에 있으면 통과. 현재값만 검증되면 충분 (수정안은 라인에 없는 게 정상).
        # - **strict default**: 그 외엔 모든 인용이 라인에 있어야 통과. phantom quote 가
        #   real quote 와 함께 한 본문에 들어가 있는 경우 ("phantom 공백 `\" usrname\"` 을
        #   `usrname` 에서 제거" 같은) 를 strict 로 잡는다. 전부 매칭이 아니면 phantom 의심.
        #
        # 이 정책은 codex review #4 의 우려 — lenient any-match 가 phantom + real 혼합을
        # 통과시킴 — 를 좁히면서, review #1 의 정상 typo+fix 패턴도 보호.
        matches = [q for q in quotes if q in line]
        if _has_fix_pattern(body_lower):
            # fix-pattern 표지 있음 → lenient: 현재값 1개만 매치되면 통과
            if matches:
                return f
        else:
            # fix-pattern 표지 없음 → strict: 모든 인용이 라인에 있어야 통과
            if len(matches) == len(quotes):
                return f
        # 매칭 실패 — phantom quote 환각으로 강등
        first_missing = next(q for q in quotes if q not in line)
        logger.warning(
            "downgrading severity %s -> Suggestion: phantom-quote in %s:%d "
            "(missing: %r, total quotes=%d, matched=%d, fix_pattern=%s). "
            "assertion-hint keyword triggered verification.",
            severity,
            f.path,
            f.line,
            first_missing,
            len(quotes),
            len(matches),
            _has_fix_pattern(body_lower),
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

    def _log_unverifiable_downgrade(self, severity: str, f: Finding, status: str) -> None:
        logger.warning(
            "downgrading severity %s -> Suggestion: cannot verify %s:%d (%s); "
            "phantom-source defense (assertion-hint keyword + missing source).",
            severity,
            f.path,
            f.line,
            status,
        )


def _has_fix_pattern(body_lower: str) -> bool:
    """body (소문자화) 에 fix-pattern 표지가 있으면 True.

    표지가 있으면 "현재값 → 수정안" 형태의 정상 finding 으로 보고 lenient 매칭 적용.
    `_FIX_PATTERN_HINTS` 에 누적된 표현 중 하나라도 등장하면 발동.
    """
    return any(hint in body_lower for hint in _FIX_PATTERN_HINTS)


def _read_source_line(
    repo_root: Path,
    path: str,
    line: int,
    line_cache: dict[str, list[str] | None],
) -> tuple[str | None, str]:
    """`repo_root / path` 의 1-based line 번호 라인을 반환.

    Returns:
        (line_text, status) 튜플:
        - 정상: ("실제 라인 텍스트", "ok")
        - 라인 못 읽음: (None, status) — status 는 호출자가 진단 메시지에 사용
            - "missing": 파일이 디스크에 없음 (삭제된 파일 등)
            - "out_of_range": 파일은 있으나 라인 번호 범위 밖
            - "traversal": path 가 repo_root 밖을 가리킴 (../ 등)
            - "io_error": 그 외 IO 오류 (권한, 바이너리 등)

    같은 verify() 호출 안에서 파일별 라인 리스트를 캐싱해 같은 파일을 여러 번 읽지 않음
    (codex PR #23 review #2). path traversal 방어 (gemini PR #23 review): `path` 는 모델
    출력에서 온 신뢰 불가 입력이므로 `..` 등으로 repo_root 밖을 가리킬 수 있다. resolve()
    후 repo_root 의 자식인지 확인 — 아니면 즉시 traversal 로 거부 (캐시도 안 함).
    """
    # Path traversal 방어 — `..` 등 repo_root 를 벗어나는 경로는 거부.
    # `resolve()` 는 symlink 도 따라가 최종 경로를 만든다. is_relative_to 로 봉쇄 검증.
    try:
        resolved_root = repo_root.resolve()
        candidate = (repo_root / path).resolve()
    except OSError:
        return None, "io_error"
    if not candidate.is_relative_to(resolved_root):
        logger.warning(
            "rejected path traversal in finding: path=%r resolved=%s outside repo_root=%s",
            path, candidate, resolved_root,
        )
        return None, "traversal"

    if path in line_cache:
        lines = line_cache[path]
    else:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
        except OSError:
            lines = None
        line_cache[path] = lines
    if lines is None:
        return None, "missing"
    if line <= 0 or line > len(lines):
        return None, "out_of_range"
    return lines[line - 1], "ok"
