#!/usr/bin/env python3
"""Tiny stand-in for the (proprietary) MWCC compiler, used ONLY by test.sh.

Understands a micro subset of C and writes a little-endian MIPS32 relocatable ELF with
one ".text" section per function (the layout mwccgap expects from MWCC):
  int NAME(void) { return N; }     -> jr $ra ; addiu $v0,$zero,N
  asm void NAME() { nop nop ... }  -> one nop per word (what mwccgap's preprocessor emits)
Anything else (comments, includes, INCLUDE_ASM lines) is ignored, like MWCC ignoring an
empty INCLUDE_ASM macro. Invoked as: fake-mwcc.py -c [flags] -o out.o in.c
"""
import re, struct, sys

args = sys.argv[1:]
out = args[args.index("-o") + 1]
src = open(args[-1], encoding="utf-8").read()

funcs = []  # (name, bytes)
pat = re.compile(
    r"int\s+(\w+)\s*\(\s*void\s*\)\s*\{\s*return\s+(\d+)\s*;\s*\}"
    r"|asm\s+void\s+(\w+)\s*\(\s*\)\s*\{([^}]*)\}"
)
for m in pat.finditer(src):
    if m.group(1):
        n = int(m.group(2))
        funcs.append((m.group(1), struct.pack("<II", 0x03E00008, 0x24020000 | n)))
    else:
        nops = len(m.group(4).split())
        funcs.append((m.group(3), bytes(4 * nops)))

shstr = b"\0"; strtab = b"\0"
def add(tab, s):
    off = len(tab); return tab + s.encode() + b"\0", off
shstr, n_text = add(shstr, ".text")
shstr, n_sym = add(shstr, ".symtab")
shstr, n_str = add(shstr, ".strtab")
shstr, n_shs = add(shstr, ".shstrtab")

symtab = bytes(16)
for i, (name, _) in enumerate(funcs):
    strtab, off = add(strtab, name)
    # st_name, st_value, st_size, st_info(GLOBAL FUNC=0x12), st_other, st_shndx
    symtab += struct.pack("<IIIBBH", off, 0, len(funcs[i][1]), 0x12, 0, i + 1)

blobs = []   # (name_off, type, flags, data, link, info, align, entsize)
for _, data in funcs:
    blobs.append((n_text, 1, 6, data, 0, 0, 16, 0))
nf = len(funcs)
blobs.append((n_sym, 2, 0, symtab, nf + 2, 1, 4, 16))
blobs.append((n_str, 3, 0, strtab, 0, 0, 1, 0))
blobs.append((n_shs, 3, 0, shstr, 0, 0, 1, 0))

body = b""; headers = bytes(40); off = 0x40
for (nm, ty, fl, data, link, info, al, es) in blobs:
    pad = (-off) % al if al > 1 else 0
    body += bytes(pad); off += pad
    headers += struct.pack("<IIIIIIIIII", nm, ty, fl, 0, off, len(data), link, info, al, es)
    body += data; off += len(data)
pad = (-off) % 4
body += bytes(pad); off += pad
ident = b"\x7fELF\x01\x01\x01" + bytes(9)
ehdr = struct.pack("<16sHHIIIIIHHHHHH", ident, 1, 8, 1, 0, 0, off, 0x70001000, 52, 0, 0, 40, len(blobs) + 1, len(blobs))
open(out, "wb").write(ehdr + bytes(12) + body + headers)
