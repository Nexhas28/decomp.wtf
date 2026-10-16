"""dehex.py FILE...: rewrite C99 hex float literals (csmith writes them; GCC 2.x can't read them) as decimal
literals that round to exactly the same value (9 significant digits for float, 17 for double)."""
import re
import sys

HEX = re.compile(r'\b0[xX]([0-9a-fA-F]*\.?[0-9a-fA-F]*)[pP]([+-]?\d+)([fFlL]?)')


def decimal(m):
    v = float.fromhex(f"0x{m.group(1)}p{m.group(2)}")
    return ("%.9g" if m.group(3) in ("f", "F") else "%.17g") % v + m.group(3)


for path in sys.argv[1:]:
    with open(path) as f:
        src = f.read()
    with open(path, "w") as f:
        f.write(HEX.sub(decimal, src))
