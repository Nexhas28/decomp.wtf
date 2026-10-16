#!/usr/bin/env bash
# Smoke test for N64Recomp (N64 MIPS -> C) and RSPRecomp (RSP microcode -> C).
# Builds a tiny big-endian MIPS32 ELF and a tiny RSP blob in a temp dir,
# runs both recompilers, and checks the generated C contains the expected lines.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
N64RECOMP="$HERE/bin/N64Recomp"
RSPRECOMP="$HERE/bin/RSPRecomp"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/n64recomp-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"

# 1. Build a tiny ELF: one function "tiny_add1" at 0x80000400 containing
#    addiu $v0,$a0,1 ; jr $ra ; nop  (big-endian MIPS32, machine 8).
python3 -I - "$TMP/tiny.elf" <<'PY'
import struct, sys
out = sys.argv[1]
TEXT_ADDR = 0x80000400
text = struct.pack(">III", 0x24820001, 0x03E00008, 0x00000000)
strtab = b"\x00tiny_add1\x00"
name_text = strtab.index(b"tiny_add1")
shstrtab = b"\x00.text\x00.symtab\x00.strtab\x00.shstrtab\x00"
def sh_name(n): return shstrtab.index(n)
EHSIZE, PHSIZE, SHSIZE = 52, 32, 40
text_off = EHSIZE + PHSIZE
sym_off = text_off + len(text)
symtab = struct.pack(">IIIBBH", 0, 0, 0, 0, 0, 0)
symtab += struct.pack(">IIIBBH", name_text, TEXT_ADDR, 12, (1 << 4) | 2, 0, 1)
str_off = sym_off + len(symtab)
shstr_off = str_off + len(strtab)
sh_off = (shstr_off + len(shstrtab) + 3) & ~3
secs = [
    struct.pack(">IIIIIIIIII", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    struct.pack(">IIIIIIIIII", sh_name(b".text"), 1, 6, TEXT_ADDR, text_off, len(text), 0, 0, 4, 0),
    struct.pack(">IIIIIIIIII", sh_name(b".symtab"), 2, 0, 0, sym_off, len(symtab), 3, 1, 4, 16),
    struct.pack(">IIIIIIIIII", sh_name(b".strtab"), 3, 0, 0, str_off, len(strtab), 0, 0, 1, 0),
    struct.pack(">IIIIIIIIII", sh_name(b".shstrtab"), 3, 0, 0, shstr_off, len(shstrtab), 0, 0, 1, 0),
]
ident = b"\x7fELF" + bytes([1, 2, 1, 0]) + bytes(8)
ehdr = ident + struct.pack(">HHIIIIIHHHHHH", 2, 8, 1, TEXT_ADDR, EHSIZE, sh_off, 0x00001001,
                           EHSIZE, PHSIZE, 1, SHSIZE, len(secs), 4)
phdr = struct.pack(">IIIIIIII", 1, text_off, TEXT_ADDR, TEXT_ADDR, len(text), len(text), 5, 0x1000)
data = ehdr + phdr + text + symtab + strtab + shstrtab
data += bytes(sh_off - len(data)) + b"".join(secs)
open(out, "wb").write(data)
PY

# 2. N64Recomp on the ELF.
mkdir -p "$TMP/n64out"
cat > "$TMP/n64.toml" <<EOF
[input]
elf_path = "$TMP/tiny.elf"
output_func_path = "$TMP/n64out"
EOF
"$N64RECOMP" "$TMP/n64.toml" > "$TMP/n64.log" 2>&1
test -f "$TMP/n64out/funcs_0.c" || { echo "FAIL: N64Recomp produced no funcs_0.c"; cat "$TMP/n64.log"; exit 1; }
grep -q "void tiny_add1(" "$TMP/n64out/funcs_0.c" || { echo "FAIL: tiny_add1 not emitted"; cat "$TMP/n64out/funcs_0.c"; exit 1; }
grep -q "ctx->r2 = ADD32(ctx->r4, 0X1);" "$TMP/n64out/funcs_0.c" || { echo "FAIL: addiu translation missing"; cat "$TMP/n64out/funcs_0.c"; exit 1; }
echo "PASS N64Recomp: tiny_add1 emitted, addiu \$v0,\$a0,1 -> ctx->r2 = ADD32(ctx->r4, 0X1)"

# 3. RSPRecomp on a tiny big-endian RSP blob: addi $8,$8,1 ; jr $ra ; nop ; nop.
python3 -I - "$TMP/rsp.bin" <<'PY'
import struct, sys
open(sys.argv[1], "wb").write(struct.pack(">IIII", 0x21080001, 0x03E00008, 0, 0))
PY
mkdir -p "$TMP/rspout"
cat > "$TMP/rsp.toml" <<EOF
text_offset = 0
text_size = 16
text_address = 0
rom_file_path = "$TMP/rsp.bin"
output_file_path = "$TMP/rspout/tiny_rsp.c"
output_function_name = "tiny_rsp"
EOF
"$RSPRECOMP" "$TMP/rsp.toml" > "$TMP/rsp.log" 2>&1
test -f "$TMP/rspout/tiny_rsp.c" || { echo "FAIL: RSPRecomp produced no output file"; cat "$TMP/rsp.log"; exit 1; }
grep -q "RspExitReason tiny_rsp(" "$TMP/rspout/tiny_rsp.c" || { echo "FAIL: tiny_rsp function not emitted"; cat "$TMP/rspout/tiny_rsp.c"; exit 1; }
grep -q "r8 = RSP_ADD32(r8, 0X1);" "$TMP/rspout/tiny_rsp.c" || { echo "FAIL: addi translation missing"; cat "$TMP/rspout/tiny_rsp.c"; exit 1; }
echo "PASS RSPRecomp: tiny_rsp emitted, addi \$8,\$8,1 -> r8 = RSP_ADD32(r8, 0X1)"
echo "PASS n64recomp smoke test (N64Recomp + RSPRecomp)"
