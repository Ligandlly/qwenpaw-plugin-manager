# -*- coding: utf-8 -*-
"""Version parsing, ordering, and compatibility gates."""

from __future__ import annotations

import pytest

from qpm.versions import (
    check_runtime_compat,
    check_sdk_compat,
    compare,
    derive_exclusive_max,
    parse_version,
    sort_versions,
)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("0.1.0", "0.1.1", -1),
        ("0.1.1", "0.1.0", 1),
        ("0.1.0", "0.1.0", 0),
        ("2.2", "2.2.0", 0),
        ("2.2.2", "2.2.2.post1", -1),
        ("2.2.2rc1", "2.2.2", -1),
        ("2.2.2a1", "2.2.2b1", -1),
        ("2.2.2b1", "2.2.2rc1", -1),
        ("2.2.2.dev1", "2.2.2a1", -1),
        ("v2.2.2", "2.2.2", 0),
        ("2.2.2+local", "2.2.2", 0),
        ("1.0.0", "2.0.0", -1),
    ],
)
def test_compare(left: str, right: str, expected: int) -> None:
    left_version = parse_version(left)
    right_version = parse_version(right)
    assert left_version is not None
    assert right_version is not None
    assert compare(left_version, right_version) == expected


def test_parse_rejects_garbage() -> None:
    for value in ("", "latest", "abc", "1.0.0-", None):
        assert parse_version(value) is None


def test_sort_versions_keeps_unknown_last() -> None:
    assert sort_versions(["0.2.0", "latest", "0.10.0", "0.2.0rc1"]) == [
        "0.2.0rc1",
        "0.2.0",
        "0.10.0",
        "latest",
    ]


def test_derive_exclusive_max() -> None:
    assert derive_exclusive_max("2.2.2") == "2.3.0"
    assert derive_exclusive_max("1.0") == "1.1.0"
    assert derive_exclusive_max("nonsense") is None


def test_runtime_compat_enforces_lower_bound_only() -> None:
    ok, note = check_runtime_compat("2.2.2", None, "2.2.1")
    assert ok is False
    assert ">=" in note

    ok, note = check_runtime_compat("2.2.2", None, "2.2.2")
    assert ok is True and note == ""

    ok, note = check_runtime_compat("2.2.2", "2.3.0", "2.4.0")
    assert ok is True
    assert "upper bound" in note

    ok, note = check_runtime_compat(None, None, None)
    assert ok is True and note == ""

    ok, note = check_runtime_compat("2.2.2", None, None)
    assert ok is True
    assert "unknown" in note


def test_sdk_compat() -> None:
    assert check_sdk_compat("1.0.0", "1.0.0") == (True, "")
    assert check_sdk_compat("", "1.0.0") == (True, "")
    assert check_sdk_compat("1.0.1", "1.0.0")[0] is False
    assert check_sdk_compat("2.0.0", "1.0.0")[0] is False
    assert check_sdk_compat("0.9.0", "1.0.0")[0] is False
    assert check_sdk_compat("not-a-version", "1.0.0")[0] is False
