#!/bin/bash
# Build the period compilers decomp.wtf ships natively for Apple Silicon Macs (macos-arm64) and pack them into
# prebuilt/, the archives the macos-arm64 parts in src/decompnet/toolchains.toml point at.
#
#   macos/toolchains/build.sh [binutils-2.6|gcc-2.7.2-kmc|gcc-2.7.2-psx|gcc-2.8.1-psx|agbcc ...]   (default: all)
#
# Needs Apple's command line tools (clang, make) and curl. Every source is pinned by sha256; with the same Xcode a
# rebuild gives the same bytes. These are the same sources as the Linux builds
# the community matches against, built the same way, with these Mac-only changes (see patches/):
#   * gcc-arm64-protos.h: GCC 2.x calls its variadic diagnostics (error, warning, pedwarn...) without prototypes;
#     on Apple arm64 that passes the arguments where the callee doesn't look, and the compiler crashed on some
#     warnings. Forced into the cc1/cc1plus objects only (the driver and cpp have their own, static, versions).
#   * glibc-qsort.c: qsort with glibc's (stable merge sort) order, linked into every program, so ties sort the
#     way they do on Linux.
#   * -ffp-contract=off: no fused multiply-add in the compilers' own arithmetic.
#   * const-double-halves.pl: floating-point constants kept in two 32-bit halves, as on the 32-bit Linux hosts
#     (completes the fix decompals/mips-gcc-2.7.2 started; without it float constants came out wrong).
#   * KMC only: the host's double word order, which xm-arm.h gets wrong for arm64.
#   * GCC 2.7.2 only: GCC 2.8's handling of NON_LVALUE_EXPR in the constant pool (see fix_272_varasm).
# Each archive is checked byte-for-byte against the Linux build by macos/toolchains/verify.sh.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/prebuilt"
PATCHES="$HERE/patches"
WORK=/tmp/decompwtf-toolchains-build   # a fixed path: it ends up in the binaries' debug info (one build at a time)
rm -rf "$WORK"; mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"   # the system compiler, not a cross one
unset CC CXX CFLAGS
export ZERO_AR_DATE=1   # no object timestamps in the link, so a rebuild gives the same bytes
JOBS="$(sysctl -n hw.ncpu)"
HOST="$(uname -m)-apple-darwin"
[ "$(uname -s)" = Darwin ] || { echo "build.sh runs on macOS" >&2; exit 1; }

COMMON_CFLAGS="-ffp-contract=off -Wno-return-type -Wno-int-conversion -Wno-incompatible-function-pointer-types"

fetch() {  # url sha256 dest
    curl -fsSL "$1" -o "$3"
    echo "$2  $3" | shasum -a 256 -c --quiet - || { echo "checksum mismatch: $1" >&2; exit 1; }
}

unpack() {  # url sha256 -> prints the source folder
    local name="${1##*/}" dir
    dir="$WORK/src-$(echo "$1" | shasum | cut -c1-8)"
    mkdir -p "$dir"
    fetch "$1" "$2" "$WORK/$name"
    tar xzf "$WORK/$name" -C "$dir" --strip-components=1
    echo "$dir"
}

# A cc that adds the glibc-ordered qsort whenever it links a program.
cc -O2 -ffp-contract=off -c "$PATCHES/glibc-qsort.c" -o "$WORK/glibc-qsort.o"
cat > "$WORK/cc" <<EOF
#!/bin/sh
for a in "\$@"; do case "\$a" in -c|-E|-S|-M|-MM) exec /usr/bin/cc "\$@";; esac; done
exec /usr/bin/cc "\$@" "$WORK/glibc-qsort.o"
EOF
chmod +x "$WORK/cc"
CCW="$WORK/cc"

pack() {  # name dir files...  -> prebuilt/<name>-macos-arm64.tar.gz, reproducible (fixed owner, times, order)
    local name="$1" dir="$2"; shift 2
    mkdir -p "$OUT"
    (cd "$dir" && find "$@" -type f -perm -u+x -exec strip -S {} \; 2>/dev/null) || true
    (cd "$dir" && find "$@" -print0 | sort -z | xargs -0 touch -h -t 200001010000 \
        && tar --format ustar --no-mac-metadata --no-xattrs --no-acls --uid 0 --gid 0 --uname root --gname root -cf - $(find "$@" ! -type d | sort) | gzip -n9 \
        > "$OUT/$name-macos-arm64.tar.gz")
    echo "$name: $(shasum -a 256 "$OUT/$name-macos-arm64.tar.gz" | cut -d' ' -f1)"
}

# GCC 2.x's cc1/cc1plus objects again, now with the prototypes forced in (the first pass built the generators,
# the driver and cpp, which must not see them).
gcc_second_pass() {  # srcdir cflags
    local d="$1" f="$2" objs
    objs=$(cd "$d" && printf 'include Makefile\nprint: ; @echo $(C_OBJS) $(OBJS) $(BC_OBJS)\n' | make -s -f - print)
    (cd "$d" && rm -f $objs cc1 cc1plus && find cp -name '*.o' ! -name 'g++.o' -delete)
    # their old unprototyped redeclarations would now conflict with the prototypes
    local srcs=() o c
    for o in $objs; do
        for c in "$d/${o%.o}.c" "$d"/config/*/"${o%.o}.c"; do [ -f "$c" ] && srcs+=("$c") && break; done
    done
    for c in "$d"/cp/*.c; do [ "${c##*/}" = g++.c ] || srcs+=("$c"); done
    # cp/errfn.c calls them through a deliberately unprototyped pointer: give each call its real shape
    sed -i '' -E -e '/^[[:space:]]*extern errorfn (error|warning|pedwarn|sprintf|error_with_file_and_line|warning_with_file_and_line|pedwarn_with_file_and_line);/d' \
        -e 's/\(\*errfn\) \(file, line, fmt/(*(void (*) (char *, int, char *, ...)) errfn) (file, line, fmt/' \
        -e 's/\(\*errfn\) \(fmt, ARGSLIST\)/(*(void (*) (char *, ...)) errfn) (fmt, ARGSLIST)/' "$d/cp/errfn.c"
    # an upstream call missing its decl argument (only reached when reporting that error)
    sed -i '' 's/error_with_decl ("implementation-reserved name `%s'"'"' used");/error_with_decl (thunk, "implementation-reserved name `%s'"'"' used");/' "$d/cp/method.c"
    # (only files that have them: touching any other, version.c say, makes make re-run configure)
    printf '%s\0' "${srcs[@]}" | xargs -0 grep -lE '^(extern[[:space:]]+)?void[[:space:]]+[a-z_]+[[:space:]]*\(\)(, error \(\))?;' \
        | tr '\n' '\0' | xargs -0 sed -i '' -E \
        -e 's/^(extern[[:space:]]+)?void[[:space:]]+(error|warning|pedwarn|fatal|sorry|really_sorry|error_with_decl|warning_with_decl|pedwarn_with_decl|error_for_asm|warning_for_asm|error_with_file_and_line|warning_with_file_and_line|pedwarn_with_file_and_line|asm_fprintf)[[:space:]]*\(\);//' \
        -e 's/^extern void pedwarn \(\), error \(\);//'

    make -C "$d" CC="$CCW" CFLAGS="$f -include $PATCHES/gcc-arm64-protos.h" cc1 cc1plus
}

# GCC 2.7.2's constant pool hashes and compares a NON_LVALUE_EXPR (from casts in static initialisers) through
# uninitialised variables; on arm64 that crashes. GCC 2.8 treats it like NOP_EXPR, which is what this does
# (verified to give the Linux build's output on every case that crashed).
fix_272_varasm() {
    sed -i '' 's/code == NOP_EXPR || code == CONVERT_EXPR)/code == NOP_EXPR || code == CONVERT_EXPR || code == NON_LVALUE_EXPR)/' "$1/varasm.c"
    [ "$(grep -c 'code == NON_LVALUE_EXPR)' "$1/varasm.c")" = 2 ] || { echo "varasm.c fix didn't apply" >&2; exit 1; }
}

build_binutils26() {  # decompals/mips-binutils-2.6: the GNU assembler of KMC GCC and the PlayStation GCCs
    local d f
    d=$(unpack https://github.com/decompals/mips-binutils-2.6/archive/54514ded39ceb32165a125ddba04ca5b551773a2.tar.gz \
        edefc8c1d77c45f28b1220fa12cdba0fb396c4c5a255f09887e6042f928c53c3)
    f="-std=gnu89 -fcommon -DDARWIN $COMMON_CFLAGS -Wno-implicit-function-declaration -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0"
    (cd "$d" && CC="$CCW" ./configure --target=mips-kmc-elf --prefix=/opt/cross --disable-gprof --disable-gdb \
        --disable-werror --host=arm-apple-darwin --build=arm-apple-darwin)
    for sub in bfd opcodes libiberty gas binutils; do make -C "$d/$sub" CC="$CCW" CFLAGS="$f"; done
    mkdir -p "$WORK/out-bu"
    cp "$d/binutils/ar" "$d/binutils/objcopy" "$d/binutils/c++filt" "$WORK/out-bu/"
    cp "$d/gas/as.new" "$WORK/out-bu/as"
    cp "$d/binutils/strip.new" "$WORK/out-bu/strip"
    pack binutils-2.6 "$WORK/out-bu" ar as c++filt objcopy strip
}

build_kmc() {  # decompals/mips-gcc-2.7.2: GCC 2.7.2 configured like the KMC compiler (N64)
    local d f
    d=$(unpack https://github.com/decompals/mips-gcc-2.7.2/archive/43d1cdb67ed135879869b5266f01efaaada5e35a.tar.gz \
        dc783433b18121f92c84af3c3bcaaa7a0230613a83c401de88d3b3aa25226fb4)
    f="-DDARWIN -std=gnu89 $COMMON_CFLAGS -Wno-implicit-function-declaration"
    (cd "$d" && CC="$CCW" ./configure --target=mips-mips-gnu --prefix=/opt/cross --with-gnu-as --disable-gprof \
        --disable-gdb --disable-werror --host=arm-apple-darwin --build=arm-apple-darwin)
    # configured as an ARM host it gets xm-arm.h, written for the old ARM FPA whose doubles store their two words
    # high first; Apple's arm64 stores them like x86, and infinities and NaNs came out wrong
    perl "$PATCHES/const-double-halves.pl" "$d"
    sed -i '' 's/^#define HOST_FLOAT_WORDS_BIG_ENDIAN 1$/#define HOST_FLOAT_WORDS_BIG_ENDIAN 0/' "$d/config/arm/xm-arm.h"
    grep -q '^#define HOST_FLOAT_WORDS_BIG_ENDIAN 0$' "$d/config/arm/xm-arm.h" || { echo "xm-arm.h fix didn't apply" >&2; exit 1; }
    touch "$d/c-parse.c"   # committed; old yacc files don't build with modern bison
    fix_272_varasm "$d"
    make -C "$d" CC="$CCW" CFLAGS="$f" xgcc cc1 cc1plus cpp cccp g++
    gcc_second_pass "$d" "$f"
    "$d/cc1" -quiet "$d/test.c" -o /dev/null
    mkdir -p "$WORK/out-kmc"
    cp "$d/xgcc" "$WORK/out-kmc/gcc"
    cp "$d/cc1" "$d/cc1plus" "$d/cpp" "$d/cccp" "$d/g++" "$WORK/out-kmc/"
    pack gcc-2.7.2-kmc "$WORK/out-kmc" cc1 cc1plus cccp cpp g++ gcc
}

build_psx() {  # $1 = 2.7.2 | 2.8.1: GNU GCC with decompals/old-gcc's PlayStation patches (old-gcc 0.17's macOS recipe)
    local v="$1" d og f host_args=() psxpatch
    og=$(unpack https://github.com/decompals/old-gcc/archive/b74211c9d959e9724802f3177c8229cd67202c87.tar.gz \
        0def8c61fc1fc80068d8952fb085a3c4dbb4069f1f362d70c698969fc3b96cc2)
    if [ "$v" = 2.7.2 ]; then
        d=$(unpack https://ftp.gnu.org/old-gnu/gcc/gcc-2.7.2.tar.gz \
            7cd8bce5c3aeec59a72ecc2d3d5123864a817b14cdbd0680b1a969c3bccc5da5)
    else
        d=$(unpack https://mirrors.kernel.org/gnu/gcc/gcc-2.8.1.tar.gz \
            3b30fbfdf93e628373d90d174243f3267b0eec9ebe792bb64fd15b8828c2ea4c)
    fi
    chmod -R u+w "$d"
    (
        cd "$d"
        for f in $(grep -l 'include <varargs\.h>' *.c); do [ -L "$f" ] || sed -i '' 's/include <varargs\.h>/include <stdarg\.h>/g' "$f"; done
        for f in gcc.c cp/g++.c; do [ -f "$f" ] && [ ! -L "$f" ] && sed -i '' 's/^extern int sys_nerr;/extern const int sys_nerr;/' "$f"; done
        if [ "$v" = 2.7.2 ]; then
            patch -u -p1 obstack.h -i "$og/patches/obstack-2.7.2.h.patch"
            patch -u -p1 configure -i "$og/patches/configure.patch"
            patch -u -p1 config/mips/mips.h -i "$og/patches/mipsel-2.7.patch"
            patch -su -p1 < "$og/patches/psx-2.5.7.patch"
            fix_272_varasm .
        else
            patch -u -p1 obstack.h -i "$og/patches/obstack-2.8.1.h.patch"
            patch -u -p1 config/mips/mips.h -i "$og/patches/mips.patch"
            patch -su -p1 < "$og/patches/psx.patch"
        fi
        perl "$PATCHES/const-double-halves.pl" .
        fetch https://raw.githubusercontent.com/gcc-mirror/gcc/74af13c174714dd3b9f1ded4b39955f003c16361/config.guess \
            7d1e3c79b86de601c3a0457855ab854dffd15163f53c91edac54a7be2e9c931b config.guess
        fetch https://raw.githubusercontent.com/gcc-mirror/gcc/6fad101f3063d722e3348d07dc93cf737f8709e4/config.sub \
            71b8d73e46e0c31b1dc91ba5306f5ef0af009273b3bb283f31d8dad69666fa9e config.sub
        chmod +x config.guess config.sub
        sed -i '' 's/| hiux\* | abug | nacl\*/| psx* \\\
'"$(printf '\t')"'     | hiux* | abug | nacl*/' config.sub
        cp "$og/patches/xm-darwin.h" config/
        awk '/^\t\*\)$/ { buf=$0; next }
             buf != "" { if (!done && /echo.*Configuration.*not supported/) {
               print "\t*-apple-darwin*)"; print "\t\txm_file=xm-darwin.h"
               print "\t\tfixincludes=Makefile.in"; print "\t\t;;"; done=1 }
               print buf; buf="" } { print }' configure > configure.tmp
        mv configure.tmp configure; chmod +x configure
    )
    f="-std=gnu89 $COMMON_CFLAGS -Wno-implicit-function-declaration -Wno-return-mismatch -w"
    [ "$v" = 2.8.1 ] && host_args=(--host="$HOST" --build="$HOST")
    (cd "$d" && CC="$CCW" CFLAGS="$f" ./configure --target=mips-sony-psx --prefix=/opt/cross --with-endian-little \
        --with-gnu-as --disable-gprof --disable-gdb --disable-werror ${host_args[@]+"${host_args[@]}"})
    [ "$v" = 2.8.1 ] && touch "$d/insn-config.h"
    make -C "$d" CC="$CCW" CFLAGS="$f" cpp cc1 xgcc cc1plus g++
    gcc_second_pass "$d" "$f"
    # old-gcc's own checks
    "$d/cc1" -quiet -O2 "$og/tests/little_endian.c" -o "$WORK/le.s" && grep -qE 'lbu\s\$2,0\(\$4\)' "$WORK/le.s"
    "$d/cc1" -quiet -O2 "$og/tests/host_wide_int.c" -o "$WORK/hwi.s" && grep -qE '# 0x80000000$' "$WORK/hwi.s"
    mkdir -p "$WORK/out-psx$v"
    cp "$d/xgcc" "$WORK/out-psx$v/gcc"
    cp "$d/cpp" "$d/cc1" "$d/cc1plus" "$d/g++" "$WORK/out-psx$v/"
    pack "gcc-$v-psx" "$WORK/out-psx$v" cc1 cc1plus cpp g++ gcc
}

build_agbcc() {  # pret/agbcc: the GBA's GCC (agbcc, old_agbcc, agbcc_arm) + pret's release include/ and lib/
    local d rel
    d=$(unpack https://github.com/pret/agbcc/archive/da598c1d918402c42c0c0d7128ba14567f3175e9.tar.gz \
        f69ae187a6133b1d353a01a86c1da92a9e38df06c6d3e08e2ae360ea051c1b60)
    make -C "$d/gcc" clean
    make -C "$d/gcc" old CC="$CCW" BASE_CFLAGS='-g -std=gnu11 -Werror-implicit-function-declaration -Wno-error=incompatible-pointer-types -ffp-contract=off'
    mv "$d/gcc/old_agbcc" "$d/"
    make -C "$d/gcc" clean
    make -C "$d/gcc" CC="$CCW" BASE_CFLAGS='-g -std=gnu11 -Werror-implicit-function-declaration -Wno-error=incompatible-pointer-types -ffp-contract=off'
    mv "$d/gcc/agbcc" "$d/"
    (cd "$d/gcc_arm" && rm -f config.status config.cache && CC="$CCW" ./configure --target=arm-elf --host=i386-linux-gnu \
        && make CC="$CCW" CFLAGS="-g -Werror-implicit-function-declaration -Wno-error=incompatible-pointer-types -std=gnu11 -ffp-contract=off" cc1)
    mkdir -p "$WORK/out-agbcc/bin"
    cp "$d/agbcc" "$d/old_agbcc" "$WORK/out-agbcc/bin/"
    cp "$d/gcc_arm/cc1" "$WORK/out-agbcc/bin/agbcc_arm"
    # include/ and lib/ are GBA code and headers, the same on every host: take them from pret's release
    rel="$WORK/agbcc-release"; mkdir -p "$rel"
    fetch https://github.com/pret/agbcc/releases/download/release/agbcc.tar.gz \
        bcf0197e3e27d8e08506a142e25fa68ea7f6679a98347a6406804eb3f9ff4d29 "$WORK/agbcc-release.tar.gz"
    tar xzf "$WORK/agbcc-release.tar.gz" -C "$rel"
    cp -R "$rel/include" "$rel/lib" "$WORK/out-agbcc/"
    pack agbcc "$WORK/out-agbcc" bin include lib
}

targets=("$@")
[ ${#targets[@]} -eq 0 ] && targets=(binutils-2.6 gcc-2.7.2-kmc gcc-2.7.2-psx gcc-2.8.1-psx agbcc)
for t in "${targets[@]}"; do
    case "$t" in
        binutils-2.6) build_binutils26 ;;
        gcc-2.7.2-kmc) build_kmc ;;
        gcc-2.7.2-psx) build_psx 2.7.2 ;;
        gcc-2.8.1-psx) build_psx 2.8.1 ;;
        agbcc) build_agbcc ;;
        *) echo "unknown toolchain: $t" >&2; exit 2 ;;
    esac
done
