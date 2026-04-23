"""GitRepoFetcher 의 fetch ref 라우팅 단위 테스트.

핵심: PullRequest.fetch_ref 가 비어있으면 head_sha 로 fallback (역호환), 비어있지 않으면
그 ref 로 fetch + FETCH_HEAD 로 checkout. fork 가 삭제된 PR 에서 base.repo 의
`refs/pull/{n}/head` 로 PR 스냅샷을 받는 경로의 회귀 방지 (codex PR #21 review #1).
"""
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gemini_review.domain import PullRequest, RepoRef
from gemini_review.infrastructure.git_repo_fetcher import GitRepoFetcher


def _make_pr(*, fetch_ref: str = "", clone_url: str = "https://example/x.git") -> PullRequest:
    return PullRequest(
        repo=RepoRef("o", "r"),
        number=42,
        title="t",
        body="",
        head_sha="abc123",
        head_ref="feat",
        base_sha="def456",
        base_ref="main",
        clone_url=clone_url,
        changed_files=("a.py",),
        installation_id=7,
        is_draft=False,
        fetch_ref=fetch_ref,
    )


def _record_subprocess_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> list[list[str]]:
    """subprocess.run 을 가로채 호출된 git 명령 시퀀스를 캡처한다.

    .git 디렉터리는 만들지 않아 clone 경로로 들어가게 하고, 모든 git 호출은 성공으로 흉내.
    """
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_checkout_uses_head_sha_when_fetch_ref_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """기본 (정상) 케이스: fetch_ref 비어있으면 head_sha 로 fetch + checkout.

    회귀 방지: 기존 (PR #21 이전) 호출부와의 호환성. fetch_ref 가 빈 문자열이라도
    `effective_fetch_ref()` 가 head_sha 로 자연 fallback 해야 한다.
    """
    calls = _record_subprocess_calls(monkeypatch, tmp_path)
    fetcher = GitRepoFetcher(cache_dir=tmp_path)

    fetcher.checkout(_make_pr(fetch_ref=""), installation_token="tkn")

    fetch_cmds = [c for c in calls if "fetch" in c and "--depth" in c]
    checkout_cmds = [c for c in calls if "checkout" in c and "--force" in c]
    assert len(fetch_cmds) == 1 and fetch_cmds[0][-1] == "abc123", (
        "fetch_ref 비어있으면 head_sha 로 fetch 해야"
    )
    assert len(checkout_cmds) == 1 and checkout_cmds[0][-1] == "abc123", (
        "fetch_ref 비어있으면 head_sha 로 checkout 해야"
    )


def test_checkout_uses_pr_ref_when_fetch_ref_set_to_pull_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """fork 삭제 fallback 시나리오: fetch_ref 가 `refs/pull/{n}/head` 로 세팅됐을 때.

    회귀 방지 (codex PR #21 review #1): clone_url 만 base 로 바꾸고 fetch 는 여전히
    head_sha 로 시도하면 base 저장소엔 그 SHA 가 없어 실패. fetch_ref 를 PR ref 로
    세팅한 PullRequest 가 들어오면 GitRepoFetcher 가 그걸 사용해 base 의 `refs/pull/`
    로 받아야 한다. 결과 SHA 는 FETCH_HEAD 에 들어가므로 checkout 도 거기로.
    """
    calls = _record_subprocess_calls(monkeypatch, tmp_path)
    fetcher = GitRepoFetcher(cache_dir=tmp_path)

    pr = _make_pr(fetch_ref="refs/pull/42/head", clone_url="https://base/x.git")
    fetcher.checkout(pr, installation_token="tkn")

    fetch_cmds = [c for c in calls if "fetch" in c and "--depth" in c]
    checkout_cmds = [c for c in calls if "checkout" in c and "--force" in c]
    assert len(fetch_cmds) == 1
    # fetch ref 는 PR ref
    assert fetch_cmds[0][-1] == "refs/pull/42/head", (
        "fetch_ref 로 PR ref 가 명시되면 그대로 git fetch 인자로 전달돼야"
    )
    # checkout 은 FETCH_HEAD (PR ref fetch 결과 SHA)
    assert len(checkout_cmds) == 1
    assert checkout_cmds[0][-1] == "FETCH_HEAD", (
        "PR ref fetch 결과는 FETCH_HEAD 에 있으므로 checkout 도 거기로 해야 한다"
    )


def test_effective_fetch_ref_falls_back_to_head_sha_when_empty() -> None:
    """도메인 헬퍼 회귀: `fetch_ref` 가 빈 문자열이면 `head_sha` 를 반환해야 한다.

    이전 호출부 (PR #21 이전 PullRequest 생성 코드) 와의 호환성. 빈 값을 명시적
    "head_sha 사용" 신호로 해석.
    """
    pr = _make_pr(fetch_ref="")
    assert pr.effective_fetch_ref() == "abc123"


def test_effective_fetch_ref_returns_explicit_value_when_set() -> None:
    """명시적 fetch_ref 가 있으면 그대로 반환 (head_sha 무시)."""
    pr = _make_pr(fetch_ref="refs/pull/42/head")
    assert pr.effective_fetch_ref() == "refs/pull/42/head"
