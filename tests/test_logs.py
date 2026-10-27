# -*- coding: utf-8 -*-
"""Unit tests for the opt-in debug logger."""

from __future__ import annotations

import io
import sys

import pytest

from qpm import logs


@pytest.fixture(autouse=True)
def clean_logger():
    logs.reset()
    try:
        yield
    finally:
        logs.reset()


def test_debug_is_off_by_default(capsys) -> None:
    logs.debug("nope")
    logs.get_logger("demo").debug("also nope")
    captured = capsys.readouterr()
    assert captured.err == ""


def test_set_enabled_writes_prefixed_lines(capsys) -> None:
    logs.set_enabled(True)
    logs.get_logger("demo").debug("hello %s", "world")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "debug: hello world\n"


def test_set_enabled_is_idempotent(capsys) -> None:
    logs.set_enabled(True)
    logs.set_enabled(True)
    logs.debug("once")
    captured = capsys.readouterr()
    assert captured.err == "debug: once\n"


def test_handler_follows_the_current_stderr(monkeypatch) -> None:
    logs.set_enabled(True)
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    logs.debug("swapped")
    assert stream.getvalue() == "debug: swapped\n"

    logs.set_enabled(False)
    logs.debug("hidden")
    assert stream.getvalue() == "debug: swapped\n"


def test_reset_restores_quiet(capsys) -> None:
    logs.set_enabled(True)
    logs.reset()
    assert logs.is_enabled() is False
    logs.debug("quiet again")
    assert capsys.readouterr().err == ""
