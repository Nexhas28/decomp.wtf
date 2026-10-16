# gclib

Python library by LagoLunatic (MIT) implementing GameCube file formats: Yaz0/Yay0 compression, RARC archives, GCM/ISO, DOL, REL, BTI textures, J3D models, BMG, BFN, JPC.
Useful for decomp.wtf when a GameCube game's assets or archives need to be unpacked/repacked, or when a Yaz0-compressed DOL/REL has to be read before splitting.

Run: `local-tools/gclib/run script.py` (runs Python inside the venv, so `import gclib` works), or `local-tools/gclib/.venv/bin/python`.
Example: `./run -c "from gclib.rarc import RARC; RARC('Stage.arc').extract_all_files_to_disk('out')"`

Installed: `uv venv` + `uv pip install git+https://github.com/LagoLunatic/gclib.git@4e06db0004df411f1c806ad39b0e9a18ac4a1de5` (package version 1.0.0, Python 3.12; pulls numpy, pillow, imagequant). The optional C speedups were not installed.
Test: `bash local-tools/gclib/test.sh` does a Yaz0 compress/decompress round trip and builds a RARC from scratch, saves it, re-reads it and extracts the file. Result on 2026-10-16: PASS.
