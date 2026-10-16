#!/bin/bash
# Check the native Mac compilers in prebuilt/ against the Linux builds the community matches against: compile the
# same sources with both, at several optimisation levels, and compare the object files byte for byte.
#
#   macos/toolchains/verify.sh [csmith-count]     (default 150 random programs per kind; needs Docker and csmith)
#
# Developer check only: the Linux side runs in Docker (linux/amd64). The corpus is zlib 1.1.4 (real 90s code),
# tests/floats.c (float constant folding and conversions), tests/warns.c (code that makes the compilers warn, the
# path that used to crash on arm64) and csmith random programs with and without floats. Only cases both sides
# compile are compared; crashes on one side are listed separately. Exit status 1 if any compiled object differs.
#
# Old GCCs hash some pointers, so on huge generated functions their output can depend on where malloc put things;
# Linux machines can then disagree among themselves too. Such a difference shows as a mismatch here and is listed.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
N="${1:-150}"
W="$(mktemp -d "${TMPDIR:-/tmp}/verify-toolchains.XXXXXX")"
W="$(cd "$W" && pwd -P)"   # Docker mounts it at the same path, so file names in the objects are the same
trap 'rm -rf "$W"' EXIT
command -v docker >/dev/null || { echo "verify.sh needs Docker for the Linux side" >&2; exit 1; }

fetch() { curl -fsSL "$1" -o "$3"; echo "$2  $3" | shasum -a 256 -c --quiet -; }

# --- the two sides ----------------------------------------------------------------------------------------------
mkdir -p "$W"/{mac,linux}/{kmc,psx272,psx281,agbcc} "$W/dl"
for t in kmc psx272 psx281; do tar xzf "$HERE/prebuilt/binutils-2.6-macos-arm64.tar.gz" -C "$W/mac/$t"; done
tar xzf "$HERE/prebuilt/gcc-2.7.2-kmc-macos-arm64.tar.gz" -C "$W/mac/kmc"
tar xzf "$HERE/prebuilt/gcc-2.7.2-psx-macos-arm64.tar.gz" -C "$W/mac/psx272"
tar xzf "$HERE/prebuilt/gcc-2.8.1-psx-macos-arm64.tar.gz" -C "$W/mac/psx281"
tar xzf "$HERE/prebuilt/agbcc-macos-arm64.tar.gz" -C "$W/mac/agbcc"

# the Linux releases toolchains.toml installs
fetch https://github.com/decompals/mips-binutils-2.6/releases/download/v0.3/binutils-2.6-linux.tar.gz \
    5a612cd28344e5b410c3344ec5dcfb92d9d03947756f190cd12404055b4a624d "$W/dl/bu.tgz"
fetch https://github.com/decompals/mips-gcc-2.7.2/releases/download/v0.1/gcc-2.7.2-linux.tar.gz \
    e09e7b53d4e47d1b7bab1f854c4498c0bffbea2553b1d7201dc3e0e983923874 "$W/dl/kmc.tgz"
fetch https://github.com/decompals/old-gcc/releases/download/0.17/gcc-2.7.2-psx.tar.gz \
    500a459b3485e885a8d302cac23c2a4632f3900e03a09153f6190699fd723571 "$W/dl/psx272.tgz"
fetch https://github.com/decompals/old-gcc/releases/download/0.17/gcc-2.8.1-psx.tar.gz \
    f6f6e883942d4d3289d048236c672e71ed410e546aaae8ff655952f1567e1be0 "$W/dl/psx281.tgz"
fetch https://github.com/pret/agbcc/releases/download/release/agbcc.tar.gz \
    bcf0197e3e27d8e08506a142e25fa68ea7f6679a98347a6406804eb3f9ff4d29 "$W/dl/agbcc.tgz"
for t in kmc psx272 psx281; do tar xzf "$W/dl/bu.tgz" -C "$W/linux/$t"; tar xzf "$W/dl/$t.tgz" -C "$W/linux/$t"; done
tar xzf "$W/dl/agbcc.tgz" -C "$W/linux/agbcc"

# --- the corpus -------------------------------------------------------------------------------------------------
C="$W/corpus"; mkdir -p "$C/zlib" "$C/src" "$C/i"
cp -R "$HERE/tests/include" "$C/include"
fetch https://zlib.net/fossils/zlib-1.1.4.tar.gz 9e3e973174f9910fd51539ef9ce94c86a3943d4f897fab8e9adf4b19e6a8291e "$W/dl/zlib.tgz"
tar xzf "$W/dl/zlib.tgz" -C "$C/zlib" --strip-components=1
for u in adler32 compress crc32 deflate infblock infcodes inffast inflate inftrees infutil trees uncompr zutil; do
    cp "$C/zlib/$u.c" "$C/src/zlib_$u.c"
done
cp "$HERE/tests/floats.c" "$HERE/tests/warns.c" "$C/src/"
if command -v csmith >/dev/null; then
    for i in $(seq 1 "$N"); do
        csmith --seed "$i" --no-safe-math --no-packed-struct -o "$C/src/cs$i.c" 2>/dev/null || true
        csmith --seed "$((i + 100000))" --float --no-safe-math --no-packed-struct --max-funcs 6 -o "$C/src/fl$i.c" 2>/dev/null || true
    done
    python3 -I "$HERE/dehex.py" "$C"/src/fl*.c
else
    echo "csmith not found (brew install csmith): checking zlib and the hand-written tests only" >&2
fi
INC=(-nostdinc -I"$C/include" -I"$C/zlib")
# agbcc compiles preprocessed C (pret preprocesses with arm-none-eabi-cpp): preprocess once on the Mac
ARMCPP="$(command -v arm-none-eabi-cpp || true)"
for f in "$C"/src/*.c; do
    [ -n "$ARMCPP" ] && "$ARMCPP" -nostdinc -undef -I"$C/include" -I"$C/zlib" "$f" -o "$C/i/$(basename "$f" .c).i" 2>/dev/null || true
done
[ -n "$ARMCPP" ] || echo "arm-none-eabi-cpp not found (brew install arm-none-eabi-gcc): skipping agbcc" >&2

# --- one script compiles everything on either side --------------------------------------------------------------
cat > "$W/run.sh" <<EOF
#!/bin/bash
side=\$1; T="$W/\$side"; O="$W/out/\$side"; mkdir -p "\$O"
mips() {  # tag flags...
    local tag=\$1; shift
    for opt in -O0 -O1 -O2 -O3 "-O2 -g"; do
        o=\$(echo "\$opt" | tr -d ' -')
        for f in "$C"/src/*.c; do
            b=\$(basename "\$f" .c)
            "\$T/\$tag/gcc" -B"\$T/\$tag/" ${INC[*]} "\$@" \$opt -c "\$f" -o "\$O/\$tag.\$b.\$o.o" 2>/dev/null \
                || rm -f "\$O/\$tag.\$b.\$o.o"
        done
    done
}
agb() {
    for f in "$C"/i/*.i; do
        [ -e "\$f" ] || continue
        b=\$(basename "\$f" .i)
        for v in "agbcc -O2" "agbcc -O0" "agbcc -O2 -g" "old_agbcc -O2" "agbcc_arm -O2"; do
            set -- \$v; c=\$1; shift
            fl=\$([ \$c = agbcc_arm ] || echo -mthumb-interwork)
            o=\$(echo "\$c\$*" | tr -d ' -')
            "\$T/agbcc/bin/\$c" \$fl "\$@" "\$f" -o "\$O/agbcc.\$b.\$o.s" 2>/dev/null || rm -f "\$O/agbcc.\$b.\$o.s"
        done
    done
}
cd "$W"
mips kmc -G0 -mips3 -mgp32 -mfp32 & mips psx272 -G8 -Wa,-EL & mips psx281 -G8 -Wa,-EL & agb; wait
EOF
chmod +x "$W/run.sh"

echo "compiling on the Mac..."
"$W/run.sh" mac
echo "compiling on Linux (Docker, linux/amd64; this takes a while)..."
docker run --rm --platform linux/amd64 -v "$W:$W" debian:13-slim bash "$W/run.sh" linux

# --- compare ----------------------------------------------------------------------------------------------------
# agbcc's .s files are assembled first: label numbers in them may differ without changing a single object byte
if [ -n "$ARMCPP" ]; then
    AS="$(dirname "$ARMCPP")/arm-none-eabi-as"
    for side in mac linux; do
        for s in "$W/out/$side"/agbcc.*.s; do
            [ -e "$s" ] && "$AS" -mcpu=arm7tdmi "$s" -o "${s%.s}.o" 2>/dev/null; rm -f "$s"
        done
    done
fi
python3 -I - "$W/out" <<'EOF'
import sys
from collections import Counter, defaultdict
from pathlib import Path
out = Path(sys.argv[1])
mac = {p.name: p for p in (out / "mac").glob("*.o")}
lin = {p.name: p for p in (out / "linux").glob("*.o")}
stats, bad = defaultdict(Counter), defaultdict(list)
for name in sorted(mac.keys() | lin.keys()):
    tc = name.split(".")[0]
    if name in mac and name in lin:
        same = mac[name].read_bytes() == lin[name].read_bytes()
        stats[tc]["identical" if same else "DIFFERENT"] += 1
        if not same:
            bad[tc].append(name)
    else:
        stats[tc]["only Mac compiled it" if name in mac else "only Linux compiled it"] += 1
for tc in sorted(stats):
    print(f"{tc:8} " + ", ".join(f"{k}: {v}" for k, v in sorted(stats[tc].items())))
    for n in bad[tc][:20]:
        print("    different:", n)
sys.exit(1 if any(s["DIFFERENT"] for s in stats.values()) else 0)
EOF
