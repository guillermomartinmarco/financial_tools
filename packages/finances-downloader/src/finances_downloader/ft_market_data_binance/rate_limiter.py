import asyncio
import time


class TokenBucket:
    """Token Bucket rate limiter thread-safe para asyncio."""

    def __init__(self, capacity: int, refill_rate: float):
        """
        Args:
            capacity: Máximo número de tokens en el bucket
            refill_rate: Tokens añadidos por segundo (float)
        """
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1) -> None:
        """Bloquea hasta que haya tokens disponibles."""
        async with self._lock:
            while self._tokens < tokens:
                await self._refill()
                if self._tokens < tokens:
                    # Calcular tiempo hasta próximo token
                    wait_time = (tokens - self._tokens) / self.refill_rate
                    await asyncio.sleep(wait_time)
            self._tokens -= tokens

    async def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        new_tokens = elapsed * self.refill_rate
        self._tokens = min(self.capacity, self._tokens + new_tokens)
        self._last_refill = now

    def available_tokens(self) -> int:
        """Retorna tokens disponibles aproximados (sin lock)."""
        return int(self._tokens)

    async def reset(self) -> None:
        """Resetea el bucket a capacidad máxima."""
        async with self._lock:
            self._tokens = float(self.capacity)
            self._last_refill = time.monotonic()


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
