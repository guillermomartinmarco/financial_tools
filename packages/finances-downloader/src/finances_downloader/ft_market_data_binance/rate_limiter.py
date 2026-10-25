"""Rate limiting de Binance (RF-5): un ``TokenBucket`` del núcleo por fuente."""

from __future__ import annotations

from finances_core.marketdata.rate_limit import TokenBucket

__all__ = ["DualTokenBucket", "TokenBucket"]


class DualTokenBucket:
    """Gestiona dos TokenBuckets: uno para Public Data, otro para REST API."""

    def __init__(
        self,
        public_capacity: int,
        public_refill: float,
        rest_capacity: int,
        rest_refill: float,
    ):
        self.public = TokenBucket(public_capacity, public_refill)
        self.rest = TokenBucket(rest_capacity, rest_refill)

    async def acquire_public(self, tokens: int = 1) -> None:
        await self.public.acquire(tokens)

    async def acquire_rest(self, tokens: int = 1) -> None:
        await self.rest.acquire(tokens)
