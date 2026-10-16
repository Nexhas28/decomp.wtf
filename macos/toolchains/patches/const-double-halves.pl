#!/usr/bin/perl
# const-double-halves.pl SRCDIR: make GCC 2.7/2.8 keep a floating-point CONST_DOUBLE the way the 32-bit Linux
# builds do, on a 64-bit host.
#
# A real value here is a host `double' (8 bytes). On the 32-bit hosts the community's Linux builds run on, a
# HOST_WIDE_INT is 4 bytes, so CONST_DOUBLE_LOW and CONST_DOUBLE_HIGH are the double's two 32-bit halves, and GCC
# moves the double in and out of them with one 8-byte copy starting at &CONST_DOUBLE_LOW. On a 64-bit host each of
# those rtx fields is 8 bytes, so that copy only fills (or reads) CONST_DOUBLE_LOW, and code that looks at the
# halves (constant pool output and sharing, moves, comparisons) saw garbage: wrong float constants. This rewrites
# every such copy to store the two 32-bit halves, as numbers, in CONST_DOUBLE_LOW and CONST_DOUBLE_HIGH, which is
# exactly what the 32-bit hosts hold. (decompals/mips-gcc-2.7.2 had fixed two of the six places.)
use strict;
use warnings;

my $dir = shift or die "usage: $0 SRCDIR\n";

sub edit {
    my ($file, @subs) = @_;
    local $/;
    open my $in, '<', "$dir/$file" or die "$dir/$file: $!";
    my $s = <$in>;
    close $in;
    for my $sub (@subs) {
        my ($name, $re, $to) = @$sub;
        $s =~ s/$re/ref $to ? $to->() : $to/e or die "$file: $name: pattern not found\n";
    }
    open my $out, '>', "$dir/$file" or die "$dir/$file: $!";
    print $out $s;
    close $out;
}

my $H0 = '((int *) &u)[0]';
my $H1 = '((int *) &u)[1]';

edit('real.h', [
    'REAL_VALUE_FROM_CONST_DOUBLE',
    qr/do \{ union real_extract u;\s*\\\n(?:\s*bcopy \([^\n]*\\\n)+\s*to = u\.d; \} while \(0\)/,
    "do { union real_extract u;\t\t\t\t\\\n"
    . "     $H0 = CONST_DOUBLE_LOW ((from));\t\t\\\n"
    . "     $H1 = CONST_DOUBLE_HIGH ((from));\t\t\\\n"
    . "     to = u.d; } while (0)",
]);

edit('emit-rtl.c', [
    'init_emit_once',
    qr/bcopy \(\(char \*\) &u, \(char \*\) &CONST_DOUBLE_LOW \(tem\), sizeof u(?: \/ 2)?\);(?:\n\s*bcopy \([^\n]*CONST_DOUBLE_HIGH \(tem\)[^\n]*\);)?/,
    "CONST_DOUBLE_LOW (tem) = $H0;\n\t  CONST_DOUBLE_HIGH (tem) = $H1;",
]);

edit('varasm.c', [
    'immed_real_const_1',
    qr/if \(sizeof u == 2 \* sizeof \(HOST_WIDE_INT\)\)\n(\s*)return immed_double_const \(u\.i\[0\], u\.i\[1\], mode\);/,
    sub { "if (sizeof u == 2 * sizeof (int))\n$1return immed_double_const ($H0, $H1, mode);" },
], [
    'decode_rtx_const',
    qr/bcopy \(\(char \*\) &CONST_DOUBLE_LOW \(x\),\s*\(char \*\) &value->un\.du, sizeof value->un\.du\);/,
    "((int *) &value->un.du)[0] = CONST_DOUBLE_LOW (x);\n\t  ((int *) &value->un.du)[1] = CONST_DOUBLE_HIGH (x);",
], [
    'output_constant_pool',
    qr/bcopy \(\(char \*\) &CONST_DOUBLE_LOW \(x\), \(char \*\) &u, sizeof u\);\n(\s*)assemble_real/,
    sub { "$H0 = CONST_DOUBLE_LOW (x);\n$1$H1 = CONST_DOUBLE_HIGH (x);\n$1assemble_real" },
]);
