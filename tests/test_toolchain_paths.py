"""A toolchain's declared command must point at a binary that is really there.

`gbdk-2020` declared `{tc}/gbdk/bin/sdcc`, but the installer's `_extract()` flattens the release
tarball into the toolchain directory and `rom.gb_build.toolchain_bin()` looks for `<tc>/bin/<name>`.
So `available()` said yes while every Game Boy compile raised

    FileNotFoundError: .../toolchains/gbdk-2020/gbdk/bin/sdcc

These tests pin the invariant for any toolchain that declares an absolute `{tc}/...` command: when the
toolchain is installed, that path must exist, and where a platform lane has its own resolver the two
must agree.
"""

from __future__ import annotations

import pytest

from pathlib import Path

from decompnet import toolchains as tc


def _declared_path(t) -> Path | None:
    if not t.cc or not str(t.cc[0]).startswith("{tc}/"):
        return None
    return Path(str(t.cc[0]).replace("{tc}", str(tc.path(t.id))))


@pytest.mark.parametrize("tid", sorted({t.id for t in tc.all()} if hasattr(tc, "all") else ["gbdk-2020"]))
def test_declared_command_exists_when_installed(tid):
    t = tc.get(tid)
    declared = _declared_path(t)
    if declared is None:
        pytest.skip(f"{tid} does not declare a {{tc}} command")
    if not tc.installed(tid):
        pytest.skip(f"{tid} is not installed")
    assert declared.exists(), f"{tid} declares {t.cc[0]}, which does not exist after install"


def test_gb_lane_resolver_agrees_with_the_declared_command():
    from decompnet.rom import gb_build

    declared = _declared_path(tc.get("gbdk-2020"))
    if declared is None or not gb_build.available():
        pytest.skip("gbdk-2020 is not installed")
    resolved = gb_build.toolchain_bin("sdcc")
    assert resolved is not None, "available() is true but sdcc cannot be resolved"
    assert resolved.samefile(declared), (
        f"gb_build resolves sdcc to {resolved}, but toolchains.toml declares {declared}: one of the two "
        "is wrong and the lane will fail at compile time")
