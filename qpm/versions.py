# -*- coding: utf-8 -*-
"""Dependency-free PEP 440-ish version parsing and comparison.

Only the subset QwenPaw actually uses is supported:

* release segments: ``2``, ``2.2``, ``2.2.2``, ``2.2.2.1``
* pre-releases: ``2.2.0a1`` / ``2.2.0-alpha.1`` / ``2.2.0rc2`` / ``2.2.0-dev``
* post releases: ``2.2.2.post1`` / ``2.2.2-1`` is *not* supported
* local versions (``+ubuntu1``) are ignored for ordering

Ordering follows packaging: ``dev < a < b < rc < final < post``.  The goal is
to let the manager decide "is the bundle newer than what is installed" without
adding ``packaging`` as a runtime dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

__all__ = [
    "Version",
    "parse_version",
    "compare",
    "derive_exclusive_max",
    "check_runtime_compat",
    "check_sdk_compat",
    "sort_versions",
]

_PRE_RANKS = {
    "dev": -4,
    "a": -3,
    "alpha": -3,
    "b": -2,
    "beta": -2,
    "c": -1,
    "rc": -1,
    "pre": -1,
    "preview": -1,
}

_VERSION_RE = re.compile(
    r"^v?"
    r"(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre>alpha|beta|preview|dev|pre|rc|a|b|c)"
    r"[-_.]?(?P<pre_num>\d+)?)?"
    r"(?:[-_.]?(?P<post>post|rev|r)[-_.]?(?P<post_num>\d+)?)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, eq=False)
class Version:
    """A comparable version with an explicit pre/post rank."""

    release: tuple[int, ...]
    rank: int = 0
    rank_num: int = 0
    text: str = field(default="", compare=False)

    def __str__(self) -> str:
        return self.text or ".".join(str(part) for part in self.release)

    @property
    def is_prerelease(self) -> bool:
        """Return whether this version is a dev/pre release."""
        return self.rank < 0

    @property
    def major(self) -> int:
        """Return the first release segment."""
        return self.release[0] if self.release else 0

    @property
    def minor(self) -> int:
        """Return the second release segment (0 when absent)."""
        return self.release[1] if len(self.release) > 1 else 0

    def _key(self) -> tuple[tuple[int, ...], int, int]:
        release = list(self.release)
        while len(release) < 3:
            release.append(0)
        return tuple(release), self.rank, self.rank_num

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return _compare_keys(self._key(), other._key()) == 0

    def __hash__(self) -> int:
        return hash(self._key())

    def __lt__(self, other: "Version") -> bool:
        return _compare_keys(self._key(), other._key()) < 0

    def __le__(self, other: "Version") -> bool:
        return _compare_keys(self._key(), other._key()) <= 0

    def __gt__(self, other: "Version") -> bool:
        return _compare_keys(self._key(), other._key()) > 0

    def __ge__(self, other: "Version") -> bool:
        return _compare_keys(self._key(), other._key()) >= 0


def _compare_keys(
    left: tuple[tuple[int, ...], int, int],
    right: tuple[tuple[int, ...], int, int],
) -> int:
    left_release, right_release = left[0], right[0]
    width = max(len(left_release), len(right_release))
    left_padded = left_release + (0,) * (width - len(left_release))
    right_padded = right_release + (0,) * (width - len(right_release))
    if left_padded != right_padded:
        return -1 if left_padded < right_padded else 1
    for index in (1, 2):
        if left[index] != right[index]:
            return -1 if left[index] < right[index] else 1
    return 0


def parse_version(value: object) -> Version | None:
    """Parse one version string, returning ``None`` when unparsable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Local version labels never affect ordering in our model.
    text = text.split("+", 1)[0]
    match = _VERSION_RE.match(text)
    if match is None:
        return None
    release = tuple(int(part) for part in match.group("release").split("."))
    pre = match.group("pre")
    post = match.group("post")
    rank = 0
    rank_num = 0
    if pre:
        rank = _PRE_RANKS[pre.lower()]
        rank_num = int(match.group("pre_num") or 0)
    elif post:
        rank = 1
        rank_num = int(match.group("post_num") or 0)
    return Version(release=release, rank=rank, rank_num=rank_num, text=text)


def compare(left: Version, right: Version) -> int:
    """Return -1, 0, or 1 for ``left`` vs ``right``."""
    return _compare_keys(left._key(), right._key())


def sort_versions(values: Iterable[str]) -> list[str]:
    """Sort version strings ascending, keeping unparsable values last."""
    parsed = [(parse_version(item), item) for item in values]
    known = sorted(
        ((version, text) for version, text in parsed if version is not None),
        key=lambda pair: pair[0]._key(),
    )
    unknown = sorted(text for version, text in parsed if version is None)
    return [text for _, text in known] + unknown


def derive_exclusive_max(min_version: str) -> str | None:
    """Mirror QwenPaw's implicit upper bound: ``2.2.x`` -> ``2.3.0``."""
    parsed = parse_version(min_version)
    if parsed is None:
        return None
    return f"{parsed.major}.{parsed.minor + 1}.0"


def check_runtime_compat(
    min_version: str | None,
    max_version: str | None,
    runtime_version: str | None,
) -> tuple[bool, str]:
    """Check a plugin's QwenPaw range against one runtime.

    Mirrors the host implementation (``_version_compat.py``): only the lower
    bound is enforced; the upper bound produces an advisory warning because
    QwenPaw currently ignores it.
    """
    if not min_version and not max_version:
        return True, ""
    if not runtime_version:
        return True, "runtime version unknown; compatibility not checked"
    current = parse_version(runtime_version)
    if current is None:
        return True, f"unparsable runtime version {runtime_version!r}"
    if min_version:
        minimum = parse_version(min_version)
        if minimum is not None and compare(current, minimum) < 0:
            return (
                False,
                f"requires QwenPaw >= {min_version}, runtime is "
                f"{runtime_version}",
            )
    if max_version:
        maximum = parse_version(max_version)
        if maximum is not None and compare(current, maximum) >= 0:
            return (
                True,
                f"runtime {runtime_version} is outside the declared "
                f"< {max_version} range (QwenPaw ignores the upper bound)",
            )
    return True, ""


def check_sdk_compat(
    sdk_version: str | None,
    host_sdk_version: str,
) -> tuple[bool, str]:
    """Check a plugin's declared SDK version against the host SDK."""
    if not sdk_version:
        return True, ""
    declared = parse_version(sdk_version)
    host = parse_version(host_sdk_version)
    if declared is None:
        return False, f"unparsable sdk.version {sdk_version!r}"
    if host is None:
        return True, f"host sdk {host_sdk_version!r} is unparsable"
    if declared.major != host.major:
        return (
            False,
            f"declares sdk {sdk_version} but the host SDK major is "
            f"{host_sdk_version}",
        )
    if compare(declared, host) > 0:
        return (
            False,
            f"declares sdk {sdk_version}, newer than host SDK "
            f"{host_sdk_version}",
        )
    return True, ""
