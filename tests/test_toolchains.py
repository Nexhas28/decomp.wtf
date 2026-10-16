import hashlib
import json
import os
import shutil
import tarfile

import pytest

from decompnet import toolchains


@pytest.fixture
def tc_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DECOMPNET_TOOLCHAINS", str(tmp_path / "toolchains"))
    return tmp_path


def test_registry_is_pinned_and_labelled():
    reg = toolchains.registry()
    assert {"gcc-2.7.2-kmc", "ee-gcc-2.96", "ido-7.1", "m2c", "runtime-i386"} <= set(reg)
    for tc in reg.values():
        assert tc.license and tc.source, tc.id
        if not tc.proprietary:  # everything we install by default is verified against a pinned hash
            assert all(p["sha256"] for p in tc.parts) or tc.commit, tc.id
    assert reg["ido-7.1"].proprietary and not reg["gcc-2.7.2-kmc"].proprietary
    assert reg["ee-gcc-2.96"].requires == ["runtime-i386"]


def test_proprietary_toolchains_need_explicit_acceptance(tc_root):
    with pytest.raises(toolchains.ToolchainError, match="--accept-license"):
        toolchains.install("ido-7.1", log=lambda *_: None)
    assert toolchains.installed("ido-7.1") is None


def test_an_arm_computer_is_told_why_nothing_compiles(tc_root, monkeypatch):
    """Issue #18: on an ARM Linux computer the games' compilers (x86 programs) don't install: the error says why."""
    monkeypatch.setattr(toolchains._platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(toolchains.sys, "platform", "linux")
    assert toolchains.current_platform() == "linux-aarch64" and not toolchains.get("ee-gcc-2.96").supported
    with pytest.raises(toolchains.ToolchainError, match=r"linux-aarch64\): decomp.wtf needs a 64-bit Intel or AMD PC"):
        toolchains.install("ee-gcc-2.96", log=lambda *_: None)
    monkeypatch.setattr(toolchains._platform, "machine", lambda: "x86_64")
    assert toolchains.unsupported_cpu() is None


def _fake(monkeypatch, tc_root, sha):
    src = tc_root / "src"
    src.mkdir()
    (src / "cc").write_text("#!/bin/sh\n")
    archive = tc_root / "fake.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        t.add(src / "cc", arcname="cc")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    fake = toolchains.Toolchain(id="fake", name="Fake", license="MIT", source="test",
                                parts=[{"url": archive.as_uri(), "sha256": sha or digest}])
    monkeypatch.setattr(toolchains, "registry", lambda: {"fake": fake})
    return digest


def test_install_verifies_checksums(tc_root, monkeypatch):
    _fake(monkeypatch, tc_root, sha="0" * 64)
    with pytest.raises(toolchains.ToolchainError, match="checksum mismatch"):
        toolchains.install("fake", log=lambda *_: None)
    assert not toolchains.path("fake").exists()


def test_install_records_a_manifest(tc_root, monkeypatch):
    digest = _fake(monkeypatch, tc_root, sha=None)
    toolchains.install("fake", log=lambda *_: None)
    m = json.loads((toolchains.path("fake") / toolchains.MANIFEST).read_text())
    assert m["parts"][0]["sha256"] == digest and (toolchains.path("fake") / "cc").exists()
    assert toolchains.require("fake") == toolchains.path("fake")


def test_zst_archives_need_no_zstd_command(tmp_path, monkeypatch):
    """The 32-bit runtime EE-GCC needs is a .tar.zst: GNU tar would run the zstd command for it, which many systems
    don't have (the install then failed with "command failed (2)")."""
    zstandard = pytest.importorskip("zstandard")
    if not shutil.which("tar"):
        pytest.skip("needs tar")
    (tmp_path / "src" / "usr" / "lib32").mkdir(parents=True)
    (tmp_path / "src" / "usr" / "lib32" / "ld-linux.so.2").write_bytes(b"\x7fELF")
    plain = tmp_path / "pkg.tar"
    with tarfile.open(plain, "w") as t:
        t.add(tmp_path / "src" / "usr", arcname="usr")
    archive = tmp_path / "pkg.tar.zst"
    archive.write_bytes(zstandard.ZstdCompressor().compress(plain.read_bytes()))
    if os.name != "nt":  # a zstd command that always fails, as a missing one does: tar must not need it
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "zstd").write_text("#!/bin/sh\nexit 2\n")
        (tmp_path / "bin" / "zstd").chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    dest = tmp_path / "out"
    dest.mkdir()
    toolchains._extract(archive, dest, "usr/lib32")
    assert (dest / "ld-linux.so.2").read_bytes() == b"\x7fELF"
    assert not list(tmp_path.glob("tmp*"))  # no leftovers


def _bundled_fake(monkeypatch, tc_root, sha):
    """A toolchain whose only part ships with decomp.wtf: an archive in $DECOMPNET_PREBUILT."""
    prebuilt = tc_root / "prebuilt"
    prebuilt.mkdir()
    (tc_root / "gcc").write_text("#!/bin/sh\n")
    archive = prebuilt / "fake-macos-arm64.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        t.add(tc_root / "gcc", arcname="gcc")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    fake = toolchains.Toolchain(id="fake", name="Fake", license="MIT", source="test",
                                parts=[{"bundled": archive.name, "sha256": sha or digest}])
    monkeypatch.setattr(toolchains, "registry", lambda: {"fake": fake})
    monkeypatch.setenv("DECOMPNET_PREBUILT", str(prebuilt))
    monkeypatch.setattr(toolchains, "_download", lambda *a: pytest.fail("a bundled part must not be downloaded"))
    return archive, digest


def test_a_bundled_part_installs_from_the_prebuilt_folder(tc_root, monkeypatch):
    archive, digest = _bundled_fake(monkeypatch, tc_root, sha=None)
    toolchains.install("fake", log=lambda *_: None)
    m = json.loads((toolchains.path("fake") / toolchains.MANIFEST).read_text())
    assert m["parts"] == [{"url": f"bundled:{archive.name}", "sha256": digest}]
    assert (toolchains.path("fake") / "gcc").exists()


def test_a_bundled_part_with_the_wrong_checksum_is_refused(tc_root, monkeypatch):
    archive, _ = _bundled_fake(monkeypatch, tc_root, sha="0" * 64)
    with pytest.raises(toolchains.ToolchainError, match="checksum mismatch for bundled:fake-macos-arm64.tar.gz"):
        toolchains.install("fake", log=lambda *_: None)
    assert not toolchains.path("fake").exists() and archive.exists()  # the shipped copy is never deleted


def test_a_missing_bundled_archive_says_so_plainly(tc_root, monkeypatch):
    archive, _ = _bundled_fake(monkeypatch, tc_root, sha=None)
    archive.unlink()
    with pytest.raises(toolchains.ToolchainError, match="doesn't include its Mac build fake-macos-arm64.tar.gz.*build.sh"):
        toolchains.install("fake", log=lambda *_: None)
    assert not toolchains.path("fake").exists()


def test_an_install_older_than_the_bundled_build_is_replaced(tc_root, monkeypatch):
    archive, digest = _bundled_fake(monkeypatch, tc_root, sha=None)
    toolchains.install("fake", log=lambda *_: None)
    assert toolchains.installed("fake")
    # a newer decomp.wtf bundles a rebuilt archive (another checksum): the old install no longer counts
    fake = toolchains.registry()["fake"]
    fake.parts[0]["sha256"] = "1" * 64
    assert toolchains.installed("fake") is None
    # nor does one from before there was a bundled build (the Rosetta download)
    fake.parts[0]["sha256"] = digest
    m = toolchains.path("fake") / toolchains.MANIFEST
    m.write_text(json.dumps({"id": "fake", "parts": [{"url": "https://example.invalid/gcc-mac.tar.gz", "sha256": "2" * 64}]}))
    assert toolchains.installed("fake") is None
    toolchains.install("fake", log=lambda *_: None)
    assert toolchains.installed("fake")["parts"] == [{"url": f"bundled:{archive.name}", "sha256": digest}]


def test_the_prebuilt_folder_defaults_to_the_checkout(monkeypatch):
    monkeypatch.delenv("DECOMPNET_PREBUILT", raising=False)
    assert toolchains.prebuilt_dir() == toolchains.Path(toolchains.__file__).resolve().parents[2] / "macos" / "toolchains" / "prebuilt"


@pytest.mark.parametrize("tid", ["gcc-2.7.2-kmc", "gcc-2.7.2-psx", "gcc-2.8.1-psx", "agbcc"])
def test_apple_silicon_gets_only_the_native_bundled_builds(tid, monkeypatch):
    parts = toolchains.get(tid).parts_for("macos-arm64")
    assert parts and all(p.get("bundled") and "url" not in p for p in parts)
    monkeypatch.setattr(toolchains, "current_platform", lambda: "macos-arm64")
    monkeypatch.setattr(toolchains.linuxbox, "wanted", lambda: True)
    assert not toolchains.get(tid).via_linux  # runs natively, not in the compiler container


def test_the_registry_pins_the_bundled_archives_in_this_checkout(monkeypatch):
    monkeypatch.delenv("DECOMPNET_PREBUILT", raising=False)
    bundled = [p for tc in toolchains.registry().values() for p in tc.parts if p.get("bundled")]
    assert bundled
    for p in bundled:  # after macos/toolchains/build.sh, copy its printed sha256s into toolchains.toml
        archive = toolchains.prebuilt_dir() / p["bundled"]
        assert archive.is_file(), archive
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == p["sha256"], p["bundled"]
