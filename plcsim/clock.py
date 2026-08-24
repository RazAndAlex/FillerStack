"""SimulationClock — orologio virtuale a passo fisso (ADR-0005).

Passo di default 1 ms; il PLC scansiona a cadenza fissa (default 10 ms).
Il clock è deterministico: non contiene RNG, avanza solo per tick.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class SimulationClock:
    """Tempo virtuale in millisecondi interi, ancorato a un timestamp di partenza."""

    def __init__(
        self,
        step_ms: float = 1.0,
        start: datetime = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
    ):
        if step_ms < 1:
            raise ValueError("step_ms deve essere >= 1")
        self.step_ms = int(step_ms)
        self.start = start
        self._ms = 0

    # -- avanzamento -------------------------------------------------------
    def tick(self) -> None:
        """Avanza di un passo (1 ms)."""
        self._ms += self.step_ms

    def jump_to(self, ms: int) -> None:
        """Salto diretto (usato per le fasi macchina vuote, ADR-0005)."""
        if ms < self._ms:
            raise ValueError("jump_to non può arretrare il clock")
        self._ms = ms

    # -- lettura -----------------------------------------------------------
    @property
    def now_ms(self) -> int:
        return self._ms

    @property
    def now(self) -> datetime:
        return self.start + timedelta(milliseconds=self._ms)

    def ts_at(self, ms: int) -> datetime:
        return self.start + timedelta(milliseconds=ms)
