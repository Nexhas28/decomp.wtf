# ndspy

Python library (RoadrunnerWMC/ndspy) for reading, editing and creating Nintendo DS file formats: whole ROMs (.nds), the NitroFS file system, LZ10 and LZ11 compression, NARC archives and ARM code sections.

Useful for decomp.wtf because DS projects often need to unpack a ROM, patch one file or code section, and repack it from a script. It is a library rather than a CLI, so use it from Python.

## How to run
- Smoke test: `bash local-tools/ndspy/test.sh`
- Ad-hoc Python: `local-tools/ndspy/run -I -c 'import ndspy.rom'`

## Install
- Source: PyPI release `ndspy` 4.2.0, matching upstream tag `v4.2.0` (commit c5016497a540144a201b46186f984e6ffc2d1a74, from GitHub API).
- Installed into `.venv/` with `uv venv` and `uv pip install ndspy==4.2.0`, Python 3.12.
- Upstream license: GPL-3.0.

## Test result
PASS (2026-10-16). `test.sh` builds a small synthetic ROM with an ARM9 blob and a NitroFS file, saves it, reloads it, patches a file and checks the change, round-trips LZ10 compression and a NARC archive, then prints `ndspy: PASS`.
