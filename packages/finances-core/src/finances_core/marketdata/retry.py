"""Política de reintentos genérica (Art. 14): backoff exponencial + *jitter*.

Cada herramienta inyecta su predicado ``is_retryable`` (p. ej. HTTP 418/429/5xx en
Binance, rate-limit de Yahoo en yfinance) y el nombre del evento de log; cada
reintento se loguea como *warning* con ``structlog`` (Art. 9).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import structlog
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_exponential_jitter,
)
from tenacity.wait import wait_base

logger = structlog.get_logger(__name__)

#: Tope de espera entre reintentos.
MAX_BACKOFF_S = 60.0

RetryPredicate = Callable[[BaseException], bool]
ErrorDescriber = Callable[[BaseException | None], Mapping[str, object]]


def build_wait(base_backoff_ms: int, *, jitter: bool = True) -> wait_base:
    """Backoff exponencial ``base * 2**(n-1)`` (+ jitter uniforme ``[0, base]``)."""
    base_s = base_backoff_ms / 1000.0
    if jitter:
        return wait_exponential_jitter(multiplier=base_s, max=MAX_BACKOFF_S, jitter=base_s)
    return wait_exponential(multiplier=base_s, max=MAX_BACKOFF_S)


def _log_retry(
    log_event: str,
    log_fields: Mapping[str, object],
    describe_error: ErrorDescriber | None,
) -> Callable[[RetryCallState], None]:
    def before_sleep(state: RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome is not None else None
        extra = dict(describe_error(exc)) if describe_error is not None else {}
        logger.warning(
            log_event,
            **log_fields,
            attempt=state.attempt_number,
            wait_s=state.next_action.sleep if state.next_action is not None else None,
            **extra,
            error=repr(exc),
        )

    return before_sleep


@dataclass(frozen=True)
class RetryPolicy:
    """Parámetros de reintento: ``max_retries`` adicionales y backoff base en ms."""

    max_retries: int
    base_backoff_ms: int
    jitter: bool = True


def build_retrying(
    policy: RetryPolicy,
    is_retryable: RetryPredicate,
    log_event: str,
    *,
    log_fields: Mapping[str, object] | None = None,
    describe_error: ErrorDescriber | None = None,
) -> AsyncRetrying:
    """Política: 1 intento + ``policy.max_retries`` reintentos, sólo si ``is_retryable(exc)``.

    Agotados los reintentos (o ante un error no reintentable) se relanza la
    excepción original (``reraise=True``).
    """
    return AsyncRetrying(
        stop=stop_after_attempt(policy.max_retries + 1),
        wait=build_wait(policy.base_backoff_ms, jitter=policy.jitter),
        retry=retry_if_exception(is_retryable),
        before_sleep=_log_retry(log_event, log_fields or {}, describe_error),
        reraise=True,
    )
