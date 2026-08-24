"""Ancoraggio temporale del run — `--start` / `--end` di `plcsim/run.py`.

Perche' esiste: l'orologio del simulatore era fisso al 2026-06-01 e la riga di
comando non lo esponeva. Uno storico serve a fare da passato, quindi la domanda
utile non e' «quando comincia» ma «quando finisce»: `--end now` fa terminare il
run adesso e ricava la partenza all'indietro da `--days`, cosi' il percorso live
puo' proseguire dall'ultimo ciclo senza un buco.

I due orologi (quello del loop e quello di Telemetry, che converte i ms
virtuali in timestamp) devono condividere l'ancoraggio: se divergessero, le due
meta' dello stesso run porterebbero date diverse.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone

from plcsim.clock import SimulationClock
from plcsim.config import SimConfig
from plcsim.run import _ancoraggio, _iso_utc, build_sim
from plcsim.telemetry import Telemetry


def test_default_resta_lancoraggio_storico():
    """Nessun parametro → None, cioe' il default di SimulationClock."""
    assert _ancoraggio(None, None, 60) is None
    assert SimulationClock().start == datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_start_esplicito():
    assert _ancoraggio("2026-01-15T06:00:00Z", None, 60) == \
        datetime(2026, 1, 15, 6, 0, tzinfo=timezone.utc)


def test_senza_fuso_e_letto_come_utc():
    """Un ISO senza fuso non deve diventare ora locale: la macchina parla UTC."""
    assert _iso_utc("2026-01-15T06:00:00").tzinfo == timezone.utc


def test_end_esplicito_calcola_la_partenza_allindietro():
    start = _ancoraggio(None, "2026-08-19T12:00:00Z", 60)
    assert start == datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc) \
        - timedelta(days=60)


def test_end_now_finisce_adesso():
    """`--end now`: la fine cade su adesso, la partenza `days` giorni prima."""
    adesso = datetime.now(timezone.utc)
    start = _ancoraggio(None, "now", 60)
    fine = start + timedelta(days=60)
    assert abs((fine - adesso).total_seconds()) < 60


def test_i_due_orologi_condividono_lancoraggio():
    s = datetime(2026, 6, 20, 15, 0, tzinfo=timezone.utc)
    cfg = SimConfig.build(seed=42)
    with tempfile.TemporaryDirectory() as d:
        tel = Telemetry(SimulationClock(start=s), d)
        clock, _plant, _plc = build_sim(cfg, tel, start=s)
        assert clock.start == tel.clock.start == s
        # e la conversione dei ms virtuali parte da li'
        assert clock.ts_at(3_600_000) == s + timedelta(hours=1)
