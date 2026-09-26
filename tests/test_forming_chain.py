"""成型链：料滴成型、剪切、合模、吹气与灌模的顺序。"""

from __future__ import annotations

import pytest

from glassmelt.errors import InterlockError


def test_short_gob_is_rejected_by_shear_and_fill(platform):
    gob = platform.gob.form(120.0)
    assert gob.formed is False
    with pytest.raises(InterlockError):
        platform.shear.feed(gob)
    with pytest.raises(InterlockError):
        platform.filler.fill(gob)
    assert platform.shear.counts()["early"] == 1
    assert platform.filler.counts()["short"] == 1


def test_formed_gob_passes_shear_and_fill(platform):
    gob = platform.gob.form(platform.config.feeder.gob_mass_target_g)
    cut = platform.shear.feed(gob)
    fill = platform.filler.fill(gob)
    assert cut.gob_index == gob.index
    assert fill.filled is True
    assert fill.short_g == pytest.approx(0.0)
    assert platform.gob.epoch == gob.epoch


def test_each_gob_is_confirmed_on_its_own(platform):
    good = platform.gob.form(platform.config.feeder.gob_mass_target_g)
    assert good.is_formed() is True
    short = platform.gob.form(100.0)
    assert short.epoch > good.epoch
    with pytest.raises(InterlockError):
        platform.shear.feed(short)


def test_blow_requires_a_closed_mold(platform):
    closure = platform.closer.closure()
    assert closure.closed is False
    with pytest.raises(InterlockError):
        platform.blow.normal(closure)
    with pytest.raises(InterlockError):
        platform.blow.retry(closure, "补料")
    assert platform.blow.counts()["early"] == 2


def test_closure_epoch_moves_when_the_mold_reopens(platform):
    closed = platform.closer.close()
    assert closed.closed is True
    assert platform.closer.is_closed() is True
    reopened = platform.closer.open_mold("脱模")
    assert reopened.closed is False
    assert reopened.epoch > closed.epoch
    assert platform.closer.last_closure().closed is False


def test_forming_cycle_runs_close_blow_fill(running):
    gob = running.gob.form(running.config.feeder.gob_mass_target_g)
    result = running.forming.run(gob, running.closer, running.blow, running.filler)
    assert result.path == "cycle"
    assert result.closed is True
    assert result.filled is True
    assert result.blown_ms > 0
    assert running.closer.is_closed() is False


def test_retry_path_reopens_and_recloses(running):
    gob = running.gob.form(running.gob.recover_mass())
    assert gob.formed is False
    filled = running.gob.form(running.config.feeder.gob_mass_target_g)
    result = running.forming.retry(
        filled, running.closer, running.blow, running.filler, "灌模不满"
    )
    assert result.path == "retry"
    assert running.forming.counts()["retries"] == 1


def test_console_blow_helper_uses_live_closure(running):
    payload = running.blow_mold(path="cycle")
    assert payload["retry"] is False
    assert payload["closure"]["closed"] is True
    assert payload["blow"]["closure_epoch"] == payload["closure"]["epoch"]
