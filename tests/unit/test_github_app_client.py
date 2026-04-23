import io
import json
import ssl
import urllib.error
import urllib.request
from typing import Any

import jwt
import pytest

from gemini_review.domain import (
    Finding,
    PullRequest,
    RepoRef,
    ReviewEvent,
    ReviewResult,
)
from gemini_review.infrastructure import github_app_client
from gemini_review.infrastructure.github_app_client import GitHubAppClient


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """urlopen 과 jwt.encode 를 가로채어 GitHubAppClient 의 호출 배선을 검증한다.

    실제 네트워크 호출이나 실 RSA 키 없이도 "어떤 URL, 어떤 TLS 컨텍스트, 어떤
    timeout 으로 호출했는가" 를 테스트에서 관찰할 수 있게 해준다.
    """
    sink: dict[str, Any] = {}

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        sink["url"] = req.full_url
        sink["timeout"] = timeout
        sink["context"] = context
        return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
    return sink


def test_request_passes_injected_tls_context_to_urlopen(captured: dict[str, Any]) -> None:
    """회귀 방지: `_request()` 는 `ssl.SSLContext` 를 `urlopen(context=...)` 로 전달해야 한다.
    이걸 빠뜨리면 python.org 빌드 Python 에서 CERTIFICATE_VERIFY_FAILED 로 파이프라인이 죽는다.
    """
    injected = ssl.create_default_context()
    client = GitHubAppClient(app_id=1, private_key_pem="-", tls_context=injected)

    client.get_installation_token(installation_id=42)

    assert captured["context"] is injected
    assert "installations/42/access_tokens" in captured["url"]


def test_default_tls_context_is_a_verifying_sslcontext(captured: dict[str, Any]) -> None:
    """기본 TLS 컨텍스트는 인증서 검증을 켠 상태여야 한다.
    (certifi 번들을 끄거나 검증을 비활성화하는 회귀를 잡는다.)
    """
    client = GitHubAppClient(app_id=1, private_key_pem="-")

    client.get_installation_token(installation_id=42)

    ctx = captured["context"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_default_tls_context_factory_is_fresh_instance() -> None:
    """생성자 기본값이 싱글톤 모듈 변수가 아니라 팩토리 함수로 만들어지는지 확인.
    덕분에 테스트·환경별로 독립된 SSLContext 를 가질 수 있다.
    """
    a = github_app_client._default_tls_context()
    b = github_app_client._default_tls_context()
    assert a is not b


def _stub_response(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    """HTTP 경로를 stub 해서 `_request_*` 경계 검증만 단독으로 테스트한다."""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_a, **_k: _FakeResponse(payload),
    )


def test_request_list_raises_when_response_is_not_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub 가 배열을 약속한 엔드포인트에서 객체/프리미티브를 반환하면 즉시 실패해야 한다."""
    _stub_response(monkeypatch, b'{"message": "rate limited"}')
    client = GitHubAppClient(app_id=1, private_key_pem="-")

    with pytest.raises(RuntimeError, match="expected JSON array"):
        client._request_list("GET", "https://api.github.com/x", auth="token t")


def test_request_list_raises_when_item_is_not_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """배열이지만 내부에 dict 가 아닌 값이 섞이면 호출부의 `f["key"]` 전에 조기 실패."""
    _stub_response(monkeypatch, b'[{"filename": "a.py"}, "broken"]')
    client = GitHubAppClient(app_id=1, private_key_pem="-")

    with pytest.raises(RuntimeError, match="expected JSON object at index 1"):
        client._request_list("GET", "https://api.github.com/x", auth="token t")


def test_request_object_raises_when_response_is_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """객체를 기대하는 엔드포인트에서 배열이 오면 마찬가지로 조기 실패."""
    _stub_response(monkeypatch, b'[1, 2, 3]')
    client = GitHubAppClient(app_id=1, private_key_pem="-")

    with pytest.raises(RuntimeError, match="expected JSON object"):
        client._request_object("GET", "https://api.github.com/x", auth="token t")


def _sample_pr() -> PullRequest:
    return PullRequest(
        repo=RepoRef("o", "r"),
        number=9,
        title="t",
        body="",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://example/x.git",
        changed_files=("a.py",),
        installation_id=7,
        is_draft=False,
    )


def _files_response(patches: dict[str, str | None]) -> bytes:
    """`/pulls/{n}/files` 응답 바디를 만들어 주는 테스트 헬퍼.

    각 파일명에 대해 `{filename, patch}` 객체 배열로 직렬화. patch=None 인 경우는
    binary/삭제/truncated 파일을 시뮬레이션하기 위함.
    """
    items = [{"filename": name, "patch": patch} for name, patch in patches.items()]
    return json.dumps(items).encode()


def _make_fake_urlopen(
    posted_bodies: list[dict[str, Any]],
    patches: dict[str, str | None],
    fail_first_review_with_422: bool = False,
):
    """post_review 흐름을 가짜 GitHub 으로 시뮬레이션하는 urlopen 대체.

    - access_tokens: 정상 토큰 응답
    - /pulls/{n}/files: 주어진 patches 를 응답
    - /pulls/{n}/reviews: posted_bodies 에 캡처. fail_first_review_with_422=True 면
      첫 호출만 422 raise.
    """

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        if "/files" in req.full_url:
            return _FakeResponse(_files_response(patches))
        # review POST
        assert req.data is not None
        posted_bodies.append(json.loads(req.data.decode("utf-8")))
        if fail_first_review_with_422 and len(posted_bodies) == 1:
            raise urllib.error.HTTPError(
                req.full_url,
                422,
                "Unprocessable Entity",
                {},  # type: ignore[arg-type]
                io.BytesIO(b'{"message": "Validation Failed"}'),
            )
        return _FakeResponse(b'{"id": 1}')

    return fake_urlopen


def test_post_review_partitions_findings_into_inline_and_surfaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """addable_lines 에 따라 finding 이 inline 과 surfaced 로 정확히 분할된다.

    핵심 회귀 방지: 사전 분할이 깨지면 (1) 422 가 다시 발생하거나 (2) 본문 surface
    가 누락되거나 (3) 같은 finding 이 두 곳에 중복 노출되는 사고가 일어난다.
    """
    posted_bodies: list[dict[str, Any]] = []
    # a.py 의 patch — 라인 5, 6 만 추가. line 42 는 diff 밖.
    patches = {"a.py": "@@ -1,0 +5,2 @@\n+x\n+y\n"}
    monkeypatch.setattr(
        urllib.request, "urlopen", _make_fake_urlopen(posted_bodies, patches)
    )
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.REQUEST_CHANGES,
        positives=("좋음",),
        improvements=("개선",),
        findings=(
            Finding(path="a.py", line=5, body="[Major] 라인 5 — 인라인 가능"),
            Finding(path="a.py", line=42, body="[Critical] 라인 42 — diff 밖"),
        ),
    )

    client.post_review(_sample_pr(), result)

    # 단일 POST — 422 retry 발동 안 함
    assert len(posted_bodies) == 1, "사전 분할이 정확하면 retry 가 일어나면 안 된다"

    # comments 에는 line 5 만
    posted = posted_bodies[0]
    assert len(posted["comments"]) == 1
    assert posted["comments"][0]["path"] == "a.py"
    assert posted["comments"][0]["line"] == 5

    # body 에 line 42 가 surface 됨
    body = str(posted["body"])
    assert "a.py:42" in body
    assert "[Critical] 라인 42" in body
    # 인라인 카운트 안내는 1건 (5 - 4 = ... 이 아니라 inline_findings 길이 = 1)
    assert "기술 단위 코멘트 1건" in body
    # surface 안내
    assert "1개 코멘트는 PR diff 범위 밖" in body


def test_post_review_all_inline_when_all_lines_addable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """모든 finding 이 addable 라인을 가리키면 surface 섹션 없이 인라인만 게시."""
    posted_bodies: list[dict[str, Any]] = []
    patches = {"a.py": "@@ -1,0 +1,3 @@\n+x\n+y\n+z\n"}  # line 1,2,3 모두 addable
    monkeypatch.setattr(
        urllib.request, "urlopen", _make_fake_urlopen(posted_bodies, patches)
    )
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        findings=(
            Finding(path="a.py", line=1, body="[Minor] x"),
            Finding(path="a.py", line=2, body="[Minor] y"),
        ),
    )

    client.post_review(_sample_pr(), result)

    body = str(posted_bodies[0]["body"])
    assert len(posted_bodies[0]["comments"]) == 2
    assert "기술 단위 코멘트 2건은 각 라인에 별도 표시" in body
    assert "드롭된 라인 지적" not in body  # surface 섹션 없음


def test_post_review_all_surfaced_when_no_addable_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """파일이 binary 등으로 patch=None 이면 모든 finding 이 surface — 인라인 0건."""
    posted_bodies: list[dict[str, Any]] = []
    patches = {"binary.png": None}  # binary file 시뮬레이션
    monkeypatch.setattr(
        urllib.request, "urlopen", _make_fake_urlopen(posted_bodies, patches)
    )
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        findings=(
            Finding(path="binary.png", line=1, body="[Minor] meta data"),
        ),
    )

    client.post_review(_sample_pr(), result)

    posted = posted_bodies[0]
    assert posted["comments"] == []
    body = str(posted["body"])
    assert "binary.png:1" in body
    assert "[Minor] meta data" in body


def test_post_review_safety_net_moves_inline_to_body_on_unexpected_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """사전 분할이 잘못 판정한 희소 케이스 (예: GitHub patch truncate) — 422 가 나면
    남은 inline 들도 body 로 옮기고 retry. 정보 손실 없이 게시 보장."""
    posted_bodies: list[dict[str, Any]] = []
    # patch 는 line 1 만 addable 이라고 알려주지만, GitHub 는 422 로 거부 (시뮬레이션)
    patches = {"a.py": "@@ -1,0 +1,1 @@\n+x\n"}
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _make_fake_urlopen(posted_bodies, patches, fail_first_review_with_422=True),
    )
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.REQUEST_CHANGES,
        findings=(
            Finding(path="a.py", line=1, body="[Critical] 우리는 addable 이라 봤지만 GitHub 가 거부"),
        ),
    )

    # 예외 삼켜져야 함 (retry 성공)
    client.post_review(_sample_pr(), result)

    assert len(posted_bodies) == 2, "1차 + retry = 2회"
    # 1차: 우리 분할 결과대로 inline 1건 시도
    assert len(posted_bodies[0]["comments"]) == 1
    # 2차: comments 비우고 그 내용을 body 로 surface
    assert posted_bodies[1]["comments"] == []
    body_retry = str(posted_bodies[1]["body"])
    assert "a.py:1" in body_retry
    assert "[Critical] 우리는 addable" in body_retry


def test_post_review_does_not_retry_when_no_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """comments 가 처음부터 비어 있었다면 422 는 다른 원인이므로 재시도하지 않는다.

    재시도가 같은 payload 로 반복되는 무한 루프를 막고, 진짜 원인(예: 잘못된 commit_id,
    invalid event) 이 로그와 예외로 드러나도록 유지한다.
    """
    call_count = 0

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        nonlocal call_count
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        call_count += 1
        raise urllib.error.HTTPError(
            req.full_url,
            422,
            "Unprocessable Entity",
            {},  # type: ignore[arg-type]
            io.BytesIO(b'{"message": "Validation Failed"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        # findings 없음
    )

    with pytest.raises(urllib.error.HTTPError) as exc:
        client.post_review(_sample_pr(), result)

    assert exc.value.code == 422
    assert call_count == 1, "재시도 없이 첫 실패에서 종료돼야 함"


def test_post_review_does_not_retry_on_non_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """422 가 아닌 다른 HTTP 에러(예: 404, 401, 500)는 그대로 전파."""
    call_count = 0

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        nonlocal call_count
        if "access_tokens" in req.full_url:
            return _FakeResponse(b'{"token": "tkn", "expires_at": ""}')
        call_count += 1
        raise urllib.error.HTTPError(
            req.full_url,
            500,
            "Internal Server Error",
            {},  # type: ignore[arg-type]
            io.BytesIO(b'{"message": "boom"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        findings=(Finding(path="a.py", line=5, body="x"),),
    )

    with pytest.raises(urllib.error.HTTPError) as exc:
        client.post_review(_sample_pr(), result)

    assert exc.value.code == 500
    assert call_count == 1
