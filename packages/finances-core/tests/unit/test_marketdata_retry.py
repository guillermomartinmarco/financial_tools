"""T1.3: política de reintentos genérica (predicado inyectado, RF-7 / Art. 14)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from finances_core.marketdata.retry import RetryPolicy, build_retrying, build_wait


class RetryableError(Exception):
    pass


class FatalError(Exception):
    pass


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, RetryableError)


async def _run(fails: list[BaseException], max_retries: int = 2) -> int:
    calls = 0
    async for attempt in build_retrying(
        RetryPolicy(max_retries=max_retries, base_backoff_ms=0),
        _is_retryable,
        "test_retry",
        log_fields={"operation": "op"},
    ):
        with attempt:
            calls += 1
            if fails:
                raise fails.pop(0)
    return calls


async def test_retries_only_retryable_errors_then_succeeds() -> None:
    with capture_logs() as logs:
        calls = await _run([RetryableError(), RetryableError()])
    assert calls == 3
    retries = [e for e in logs if e["event"] == "test_retry"]
    assert [e["attempt"] for e in retries] == [1, 2]
    assert all(e["operation"] == "op" and e["log_level"] == "warning" for e in retries)


async def test_non_retryable_error_is_not_retried() -> None:
    with capture_logs() as logs, pytest.raises(FatalError):
        await _run([FatalError(), RetryableError()])
    assert logs == []


async def test_exhausted_retries_reraise_original() -> None:
    with pytest.raises(RetryableError):
        await _run([RetryableError(), RetryableError(), RetryableError()], max_retries=2)


async def test_describe_error_adds_fields() -> None:
    with capture_logs() as logs:
        async for attempt in build_retrying(
            RetryPolicy(max_retries=1, base_backoff_ms=0),
            _is_retryable,
            "test_retry",
            describe_error=lambda exc: {"kind": type(exc).__name__},
        ):
            with attempt:
                if not logs:
                    raise RetryableError
    assert logs[0]["kind"] == "RetryableError"


def test_build_wait_exponential_without_jitter() -> None:
    wait = build_wait(1000, jitter=False)
    assert wait(SimpleNamespace(attempt_number=3)) == pytest.approx(4.0)


def test_build_wait_is_capped() -> None:
    wait = build_wait(1000, jitter=False)
    assert wait(SimpleNamespace(attempt_number=20)) == pytest.approx(60.0)
