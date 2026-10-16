#!/usr/bin/env bash
# Smoke test: build a tiny SDK-shaped DS ROM (ARM9 with build-info, ITCM/DTCM autoloads and
# nitrocode footer) using ../ndstool + arm-none-eabi-gcc, then dsrom extract -> build ->
# extract -> build and check the round trip is stable and the content survives.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
D="$here/bin/dsrom"
NT="$here/../ndstool/bin/ndstool"
[ -x "$NT" ] || { echo "needs local-tools/ndstool"; exit 1; }
command -v arm-none-eabi-gcc >/dev/null || { echo "needs arm-none-eabi-gcc (brew)"; exit 1; }
export RUST_BACKTRACE=0 RUST_LOG=warn
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
cat > a9.s <<'S'
.arm
.global _start
_start:
  b main
  .space 0x800                @ zero-filled "secure area" so ds-rom treats it as decrypted
main:
  mov r0, #0x2A
  b main
build_info:
  .word ai_start, ai_end, blocks, blocks, blocks, 0, 0x05027533, 0xdec00621, 0x2106c0de
blocks:
  .word 0xe12fff1e            @ ITCM autoload: bx lr
  .word 0x11223344            @ DTCM autoload data
ai_start:
  .word 0x01ff8000, 4, 0
  .word 0x027e0000, 4, 0
ai_end:
S
printf '.arm\n.global _start\n_start:\n  mov r1, #0x07\n  b _start\n' > a7.s
arm-none-eabi-gcc -nostdlib -Wl,-Ttext=0x02000000 -Wl,-e,_start a9.s -o a9.elf
arm-none-eabi-gcc -nostdlib -Wl,-Ttext=0x02380000 -Wl,-e,_start a7.s -o a7.elf
arm-none-eabi-objcopy -O binary a9.elf a9.bin
arm-none-eabi-objcopy -O binary a7.elf a7.bin
# 32x32 4bpp banner icon
python3 -I -c '
import struct
pal=b"".join(struct.pack("<BBBB",i*16,i*16,i*16,0) for i in range(16))
pix=bytes(((x//4)%16)*17 for y in range(32) for x in range(0,32,2))
dib=struct.pack("<IiiHHIIiiII",40,32,32,1,4,0,len(pix),2835,2835,16,16)
off=14+40+len(pal)
open("icon.bmp","wb").write(b"BM"+struct.pack("<IHHI",off+len(pix),0,0,off)+dib+pal+pix)'
# 156-byte header logo: a self-made checkerboard compressed with ds-rom's Logo::compress
# (generator: fixtures/logogen.rs). Not the Nintendo logo.
printf '\x00\xf0\xae\x51\xe0\xff\x6f\x0f\xdf\xfe\x1f\x00\x3f\x00\xc0\xff\x80\xff\xbf\xfd\x7f\xfb\x7f\x00\xff\x00\x00\xff\x00\xfe\xff\xf6\xff\xed\xff\x01\xff\x03\x00\xfc\x07\x00\xf8\xdb\x00\xf0\xff\xb7\xff\x6f\xff\x0f\xfe\x1f\x00\xe0\x00\xc0\xff\xdf\xff\xbf\xfd\x3f\xfb\x7f\x00\x80\x00\x00\xff\x7f\xfe\xff\xf6\xff\xed\xff\x01\x00\x80\x10\x6a\xfc\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00' > logo.bin
mkdir -p fs/data && echo hello-decomp > fs/data/hello.txt
"$NT" -c t.nds -9 a9.bin -7 a7.bin -r9 0x02000000 -e9 0x02000000 -r7 0x02380000 -e7 0x02380000 \
       -d fs -b icon.bmp "Decomp;WTF;Test" -o logo.bin -g DWTF 01 DECOMPWTF >/dev/null 2>&1
# ARM9 footer right after the ARM9 binary: nitrocode, build-info offset, 0
BO=$(( 0x$(arm-none-eabi-nm a9.elf | awk '/ build_info$/{print $1}') - 0x02000000 ))
OFF=$(( 0x200 + $(wc -c < a9.bin) ))
printf "\x21\x06\xc0\xde$(printf '\\x%02x\\x%02x' $((BO&255)) $((BO>>8&255)))\x00\x00\x00\x00\x00\x00" \
  | dd of=t.nds bs=1 seek=$OFF conv=notrunc 2>/dev/null

"$D" dump --rom t.nds header > hdr.txt 2>&1
grep -q DECOMPWTF hdr.txt
"$D" extract --rom t.nds --path x1 >/dev/null 2>&1
[ "$(cat x1/files/data/hello.txt)" = "hello-decomp" ]
[ "$(xxd -p x1/arm9/itcm.bin)" = "1eff2fe1" ]
[ "$(xxd -p x1/arm9/dtcm.bin)" = "44332211" ]
[ -f x1/header_logo.png ] && [ -f x1/banner/bitmap.png ]
"$D" build --config x1/config.yaml --rom r1.nds >/dev/null 2>&1
"$D" extract --rom r1.nds --path x2 >/dev/null 2>&1
"$D" build --config x2/config.yaml --rom r2.nds >/dev/null 2>&1
cmp r1.nds r2.nds
cmp x1/arm9/arm9.bin x2/arm9/arm9.bin
cmp x1/arm7/arm7.bin x2/arm7/arm7.bin
echo "ds-rom: PASS"
