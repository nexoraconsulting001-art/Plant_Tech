"""Tracebacks in logs carry no local variables, in JSON (staging, production) and on the console (dev).

structlog's defaults render them (dict_tracebacks: SHOW_LOCALS = True; the console: rich with locals). In the load
test of 3 Nov one console traceback was ~8,600 lines and ~10 s of CPU, and locals hold PINs, tokens and phones."""
import json
import logging
import time

import pytest
import structlog

import observability
from settings import Settings

PIN = "48" + "2913"  # not written out as one literal: tracebacks also show source lines


def _fail(depth: int = 0):
    secret_pin = PIN  # noqa: F841 (a local the traceback must not show)
    big = {"rows": list(range(5000))}  # noqa: F841
    if depth < 20:
        _fail(depth + 1)
    raise ValueError("boom")


def _logged(capsys, settings: Settings) -> str:
    observability.setup_logging(settings)
    try:
        capsys.readouterr()
        try:
            _fail()
        except ValueError:
            logging.getLogger("tests.tracebacks").exception("it failed")
        try:
            _fail()
        except ValueError:
            structlog.get_logger("tests.tracebacks").exception("it failed again")
        return capsys.readouterr().out
    finally:
        observability.setup_logging()


@pytest.mark.parametrize("fmt", ["json", "console"])
def test_no_locals_in_tracebacks(capsys, fmt):
    start = time.perf_counter()
    out = _logged(capsys, Settings(log_format=fmt))
    took = time.perf_counter() - start
    assert "boom" in out and "_fail" in out  # the traceback itself is there
    assert PIN not in out and "secret_pin" not in out and "4999" not in out
    assert took < 2, took
    assert len(out.splitlines()) < 300  # two tracebacks of 22 frames, not thousands of lines
    if fmt == "json":
        rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        assert len(rows) == 2
        for row in rows:
            frames = row["exception"][0]["frames"]
            assert len(frames) >= 20 and all("locals" not in f for f in frames)
            assert {"filename", "lineno", "name"} <= set(frames[0])  # the same frames as before, without locals
    else:
        assert "Traceback (most recent call last)" in out and "locals" not in out


def test_dev_default_is_the_plain_console(capsys):
    out = _logged(capsys, Settings(env="dev", log_format=""))
    assert "Traceback (most recent call last)" in out and PIN not in out
