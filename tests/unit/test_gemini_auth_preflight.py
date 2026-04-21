import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gemini_review.domain import FileDump, PullRequest, RepoRef
from gemini_review.infrastructure.gemini_cli_engine import GeminiAuthError, GeminiCliEngine


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _engine(creds: Path) -> GeminiCliEngine:
    return GeminiCliEngine(binary="gemini", model="gemini-2.5-pro", oauth_creds_path=creds)


def _write_good_creds(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"refresh_token": "abc", "access_token": "xyz", "token_uri": "..."}),
        encoding="utf-8",
    )


def _sample_pr() -> PullRequest:
    return PullRequest(
        repo=RepoRef("o", "r"),
        number=1,
        title="title",
        body="",
        head_sha="abc",
        head_ref="feature",
        base_sha="def",
        base_ref="main",
        clone_url="https://example.com/o/r.git",
        changed_files=("src/a.py",),
        installation_id=7,
        is_draft=False,
    )


def test_verify_auth_passes_with_binary_and_creds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "oauth_creds.json"
    _write_good_creds(creds)

    def fake_run(*_args: Any, **_kwargs: Any) -> _FakeCompleted:
        return _FakeCompleted(0, "0.1.11\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    status = _engine(creds).verify_auth()
    assert status.startswith("gemini ")
    assert "oauth_creds.json" in status


def test_verify_auth_raises_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "oauth_creds.json"
    _write_good_creds(creds)

    def fake_run(*_args: Any, **_kwargs: Any) -> _FakeCompleted:
        raise FileNotFoundError("gemini: not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(GeminiAuthError) as exc:
        _engine(creds).verify_auth()
    assert "GEMINI_BIN" in str(exc.value)


def test_verify_auth_raises_on_binary_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "oauth_creds.json"
    _write_good_creds(creds)

    def fake_run(*_args: Any, **_kwargs: Any) -> _FakeCompleted:
        raise subprocess.TimeoutExpired(cmd="gemini", timeout=10)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(GeminiAuthError) as exc:
        _engine(creds).verify_auth()
    assert "10초" in str(exc.value)


def test_verify_auth_raises_when_creds_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 바이너리 프로브는 통과 — 오직 creds 파일 부재로만 실패하도록 구성.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(0, "0.1.11\n"))
    missing = tmp_path / "does_not_exist.json"
    with pytest.raises(GeminiAuthError) as exc:
        _engine(missing).verify_auth()
    assert "로그인" in str(exc.value)
    assert str(missing) in str(exc.value)


def test_verify_auth_raises_when_creds_corrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(0, "0.1.11\n"))
    creds = tmp_path / "oauth_creds.json"
    creds.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(GeminiAuthError) as exc:
        _engine(creds).verify_auth()
    assert "읽지 못했습니다" in str(exc.value)


def test_verify_auth_raises_when_refresh_token_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(0, "0.1.11\n"))
    creds = tmp_path / "oauth_creds.json"
    creds.write_text(json.dumps({"access_token": "xyz"}), encoding="utf-8")
    with pytest.raises(GeminiAuthError) as exc:
        _engine(creds).verify_auth()
    assert "refresh_token" in str(exc.value)


def test_verify_auth_raises_on_binary_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "oauth_creds.json"
    _write_good_creds(creds)

    def fake_run(*_args: Any, **_kwargs: Any) -> _FakeCompleted:
        return _FakeCompleted(1, "", "boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(GeminiAuthError) as exc:
        _engine(creds).verify_auth()
    assert "실행에 실패" in str(exc.value)


def test_review_invokes_prompt_mode_with_stdin_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return _FakeCompleted(
            0,
            '{"summary": "ok", "event": "COMMENT", "comments": []}',
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GeminiCliEngine(binary="gemini", model="gemini-2.5-pro").review(
        _sample_pr(),
        FileDump(entries=(), total_chars=0),
    )

    assert captured["cmd"] == ["gemini", "-m", "gemini-2.5-pro", "-p", " "]
    assert "=== PR METADATA ===" in str(captured["input"])
    assert result.summary == "ok"


def test_review_falls_back_when_preview_model_capacity_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        calls.append(cmd)
        if len(calls) == 1:
            return _FakeCompleted(
                1,
                stderr=(
                    "429 RESOURCE_EXHAUSTED: "
                    "No capacity available for model gemini-3.1-pro-preview"
                ),
            )
        return _FakeCompleted(
            0,
            '{"summary": "fallback ok", "event": "COMMENT", "comments": []}',
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GeminiCliEngine(
        binary="gemini",
        model="gemini-3.1-pro-preview",
        fallback_models=("gemini-2.5-pro",),
    ).review(_sample_pr(), FileDump(entries=(), total_chars=0))

    assert [cmd[2] for cmd in calls] == [
        "gemini-3.1-pro-preview",
        "gemini-2.5-pro",
    ]
    assert result.summary == "fallback ok"


def test_review_does_not_fall_back_on_non_retryable_cli_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        calls.append(cmd)
        return _FakeCompleted(1, stderr="OAuth login required")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="OAuth login required"):
        GeminiCliEngine(
            binary="gemini",
            model="gemini-3.1-pro-preview",
            fallback_models=("gemini-2.5-pro",),
        ).review(_sample_pr(), FileDump(entries=(), total_chars=0))

    assert [cmd[2] for cmd in calls] == ["gemini-3.1-pro-preview"]
