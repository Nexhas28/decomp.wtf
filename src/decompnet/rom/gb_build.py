"""Game Boy / Game Boy Color C build lane: SDCC (via the pinned GBDK-2020 toolchain) to SM83 bytes.

The fork's other lanes compile a C file to an ELF object and read the function out of it with
`elfobj`. SM83 has no ELF toolchain in this pipeline, so this module runs the same conceptual steps
with SDCC's own tools and produces the same thing — a function's exact machine code:

    sdcc -msm83 -S   func.c        # C  -> SM83 assembly
    sdasgb -l        func.asm      # assembly -> .rel + a listing (label offsets AND instruction bytes)
    (parse the listing)            # per-function bytes

Addresses: a Game Boy ROM's bytes contain absolute call/jump addresses, so matching byte-for-byte
needs the *same* addresses the ROM has. Callers pass the project's symbol table (`symbols`: name ->
address) and this module injects `.equ _name, 0xADDR` before assembling, which makes SDCC's references
resolve to the ROM's real values — the strongest form of comparison, with nothing masked. Symbols we do
not know are reported rather than silently assembled as zero.

Everything here is deterministic and side-effect free apart from its temp directory, so the same
(source, header, symbols, flags) always yields the same bytes.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .n64 import RomError

# A compiled-candidate payload: the fork's compile path returns bytes, so the GB lane labels its JSON
# with this magic and `elfobj.function_from_payload` reads it back.
PAYLOAD_MAGIC = b"GBOBJ1\n"

#: The toolchain id used for the Game Boy C compiler (see toolchains.toml).
TOOLCHAIN = "gbdk-2020"

_LABEL = re.compile(r"^\s*([0-9A-Fa-f]{8})\s+\d+\s+([._A-Za-z][\w.$]*):{1,2}\s*$")
_LINE = re.compile(r"^\s*([0-9A-Fa-f]{8})\s+((?:[0-9A-Fa-f]{2}\s+)+)\s*\[\s*\d+\]\s+\d+\s+(.*)$")
_SYM_FIX = re.compile(r"^([._A-Za-z][\w.$]*)$")


@dataclass
class BuiltFunction:
    """One C function as the assembler encoded it."""

    name: str
    code: bytes
    start: int = 0
    relocs: list = field(default_factory=list)
    asm: str = ""

    def __post_init__(self) -> None:
        self.name = self.name[1:] if self.name.startswith("_") else self.name  # SDCC's C symbol prefix


def toolchain_dirs() -> list[Path]:
    """Where the GBDK toolchain is installed (empty when it isn't).

    The fork's installer extracts the release exactly as published, and GBDK's tarball has a single
    top-level `gbdk/` directory — so the binaries live either at `<root>/bin` or `<root>/gbdk/bin`
    depending on how the archive was unpacked. Both are accepted."""
    roots: list[Path] = []
    try:
        from .. import toolchains as tc

        roots.append(Path(tc.path(TOOLCHAIN)))
    except Exception:
        pass
    roots += [Path.home() / "Library/Application Support/decompnet/toolchains" / TOOLCHAIN,
              Path.home() / ".local/share/decompnet/toolchains" / TOOLCHAIN]
    dirs: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for cand in [root] + sorted(p for p in root.iterdir() if p.is_dir()):
            if (cand / "bin").is_dir():
                dirs.append(cand)
    return dirs


def toolchain_bin(name: str) -> Path | None:
    """A binary inside the installed GBDK toolchain, or None when the toolchain isn't installed."""
    for d in toolchain_dirs():
        exe = d / "bin" / name
        if exe.exists():
            return exe
    return None


def available() -> bool:
    return all(toolchain_bin(n) for n in ("sdcc", "sdasgb"))


def toolchain_root() -> Path | None:
    exe = toolchain_bin("sdcc")
    return exe.parent.parent if exe else None


def _run(argv: list[str], cwd: Path, timeout: float = 120.0) -> tuple[int, str]:
    try:
        res = subprocess.run([str(a) for a in argv], cwd=str(cwd), capture_output=True, text=True,
                             timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s: {' '.join(str(a) for a in argv)}"
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def _equates(symbols: dict[str, int] | None, defined: set[str] | None = None) -> str:
    """`.equ` lines so SDCC's references resolve to the ROM's own addresses (absolute mode).

    Symbols the candidate itself defines are skipped: an `.equ` for a name the assembly also labels
    (the function being compiled is in the project's own symbol table) is a duplicate definition and
    makes the assembler report "label location changing between passes"."""
    if not symbols:
        return ""
    lines = []
    for name, addr in sorted(symbols.items()):
        if not _SYM_FIX.match(name):
            continue
        if defined and (name in defined or f"_{name}" in defined):
            continue
        symbol = name if name.startswith("_") else f"_{name}"
        lines.append(f"\t.equ {symbol}, 0x{int(addr) & 0xFFFF:04X}")
    return "\n".join(lines)


def parse_listing(text: str) -> dict[str, BuiltFunction]:
    """Functions out of an `sdasgb -l` listing: each label starts a function, its instructions run to
    the next label (trimmed after the terminating instruction, so trailing padding/next-function bytes
    never leak in)."""
    funcs: dict[str, BuiltFunction] = {}
    current: str | None = None
    code = bytearray()
    asm: list[str] = []
    order: list[str] = []

    def flush() -> None:
        nonlocal current, code, asm
        if current is not None and code:
            built = BuiltFunction(name=current, code=bytes(code), asm="\n".join(asm))
            funcs[built.name] = built  # canonical name (SDCC's leading underscore stripped)
        current, code, asm = None, bytearray(), []

    for raw in text.splitlines():
        if raw.startswith("ASxxxx") or "Symbol Table" in raw or raw.startswith("Hexadecimal"):
            continue
        label = _LABEL.match(raw)
        if label:
            flush()
            current = label.group(2)
            order.append(current)
            continue
        if current is None:
            continue
        line = _LINE.match(raw)
        if not line:
            continue
        for byte in line.group(2).split():
            code.append(int(byte, 16))
        asm.append(line.group(3).strip())
        if re.match(r"^\s*(reti?|jp\s+hl|jr\s)", line.group(3)) and not line.group(3).strip().startswith("call"):
            flush()  # a function ends at its terminating instruction
    flush()
    return funcs


def build_source(source: str, *, context_header: str = "", include_dirs: tuple[str, ...] = (),
                 symbols: dict[str, int] | None = None, opt: str = "--opt-code-speed",
                 timeout: float = 120.0) -> dict[str, BuiltFunction]:
    """Compile candidate C and return every function it defines, with its exact SM83 bytes.

    Raises RomError with the compiler/assembler output when the source cannot be built — a build
    failure is information for the caller, never a silent empty result."""
    sdcc, sdasgb = toolchain_bin("sdcc"), toolchain_bin("sdasgb")
    if sdcc is None or sdasgb is None:
        raise RomError(f"the Game Boy C toolchain ({TOOLCHAIN}) is not installed: run `decomp setup`")
    # The same source policy every other lane applies (compiler.check_source): matches must come from C,
    # so inline assembly, `__naked`, pragmas and system includes are refused here too. Without this the
    # Game Boy lane would be a loophole: `__asm("nop")` reproduces any single-instruction target.
    from ..compiler import check_source

    check_source(source)
    root = toolchain_root()
    with tempfile.TemporaryDirectory(prefix="gb-build-") as tmp:
        work = Path(tmp)
        inc = work / "include"
        inc.mkdir()
        for d in include_dirs:
            p = Path(d)
            if p.exists():
                shutil.copytree(p, inc, dirs_exist_ok=True)
        if context_header:
            (inc / "context.h").write_text(context_header, encoding="utf-8", newline="\n")
            body = '#include "context.h"\n#line 1 "func.c"\n' + source + "\n"
        else:
            body = source + "\n"
        (work / "func.c").write_text(body, encoding="utf-8", newline="\n")

        argv = [sdcc, "-msm83", "-S", "-o", "func.asm"]
        if opt:
            argv.append(opt)
        for d in (inc, root / "include" if root else None):
            if d is not None:
                argv += ["-I", str(d)]
        argv.append("func.c")
        rc, log = _run(argv, work, timeout)
        if rc != 0 or not (work / "func.asm").exists():
            raise RomError(f"sdcc could not compile the candidate: {log.strip()[-600:]}")

        # inject the ROM's addresses, then assemble to a listing (label offsets + instruction bytes)
        asm_text = (work / "func.asm").read_text(encoding="utf-8", errors="replace")
        defined = set(re.findall(r"^\s*(_[\w.$]+)::", asm_text, re.M))  # labels this candidate defines
        equates = _equates(symbols, defined)
        if equates:
            lines = asm_text.splitlines()
            for i, line in enumerate(lines):
                if line.strip().startswith(".area"):
                    lines.insert(i, equates)
                    break
            else:
                lines.insert(0, equates)
            asm_text = "\n".join(lines) + "\n"
        (work / "func.asm").write_text(asm_text, encoding="utf-8", newline="\n")

        rc, log = _run([sdasgb, "-l", "-o", "func.rel", "func.asm"], work, timeout)
        lst = work / "func.lst"
        if rc != 0 or not lst.exists():
            raise RomError(f"sdasgb could not assemble the candidate: {log.strip()[-600:]}")
        # An external reference the assembler could not resolve assembles as zeros. That is exactly the
        # kind of "match" nobody wants, so refuse it and name the symbols (pass them in `symbols` with
        # the project's addresses to resolve them, which is how byte-identical matching works).
        rel = work / "func.rel"
        if rel.exists():
            unresolved = sorted({m.group(1).lstrip("_")
                                 for m in re.finditer(r"^S\s+(\S+)\s+Ref", rel.read_text(encoding="utf-8",
                                                                                      errors="replace"), re.M)})
            if unresolved:
                raise RomError("the candidate calls symbols this build does not know: "
                               + ", ".join(unresolved) + " (pass their addresses in `symbols`)")
        funcs = parse_listing(lst.read_text(encoding="utf-8", errors="replace"))
        if not funcs:
            raise RomError("the candidate compiled but defined no function (nothing to compare)")
        return funcs


def build_payload(source: str, *, name: str | None = None, **kw) -> bytes:
    """The compile-pipeline form: a magic-tagged JSON payload of every function the source defines."""
    funcs = build_source(source, **kw)
    payload = {n: {"code": f.code.hex(), "start": f.start, "relocs": [list(r) for r in f.relocs],
                   "asm": f.asm}
               for n, f in funcs.items()}
    if name is not None and name not in payload:
        raise RomError(f"the candidate did not define {name!r} (defined: {', '.join(sorted(funcs))})")
    return PAYLOAD_MAGIC + json.dumps({"arch": "sm83", "functions": payload}).encode("utf-8")


def parse_payload(payload: bytes) -> dict:
    if not payload.startswith(PAYLOAD_MAGIC):
        raise RomError("not a Game Boy candidate payload")
    return json.loads(payload[len(PAYLOAD_MAGIC):].decode("utf-8"))


def compile_function(source: str, name: str, *, symbols: dict[str, int] | None = None,
                     context_header: str = "", **kw):
    """One function's bytes (the shape scoring wants), by name."""
    from ..elfobj import FunctionObj

    funcs = build_source(source, context_header=context_header, symbols=symbols, **kw)
    key = name if name in funcs else (f"_{name}" if f"_{name}" in funcs else None)
    if key is None:
        raise RomError(f"the candidate did not define {name!r} (defined: {', '.join(sorted(funcs))})")
    built = funcs[key]
    return FunctionObj(name=name, arch="sm83", big_endian=False, code=built.code,
                       relocs=list(built.relocs))


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point: `python -m decompnet.rom.gb_build --src f.c --name f [--symbols s.json]`."""
    import argparse

    ap = argparse.ArgumentParser(description="compile Game Boy C to SM83 bytes (SDCC/GBDK-2020)")
    ap.add_argument("--src", required=True, help="C file to compile")
    ap.add_argument("--name", help="function to print (default: every function found)")
    ap.add_argument("--symbols", help="JSON {name: address} of the ROM's symbols (absolute mode)")
    ap.add_argument("--header", help="context header file to expose as context.h")
    ap.add_argument("--include", action="append", default=[], help="extra include directory")
    ap.add_argument("--json", action="store_true", help="print the payload as JSON instead")
    args = ap.parse_args(argv)

    symbols = json.loads(Path(args.symbols).read_text(encoding="utf-8")) if args.symbols else None
    header = Path(args.header).read_text(encoding="utf-8") if args.header else ""
    source = Path(args.src).read_text(encoding="utf-8")
    if args.json:
        payload = build_payload(source, name=args.name, symbols=symbols, context_header=header,
                                include_dirs=tuple(args.include))
        print(json.dumps(parse_payload(payload), indent=1))
        return 0
    funcs = build_source(source, symbols=symbols, context_header=header, include_dirs=tuple(args.include))
    for fname, fn in sorted(funcs.items()):
        if args.name and fname not in (args.name, f"_{args.name}"):
            continue
        print(f"{fname}: {len(fn.code)} bytes  {fn.code.hex()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
