"""Ghidra p-code for some bytes, run by localtools with pypcode's own Python (local-tools/pypcode/.venv).

Usage: python -I localtools_pcode.py <language id> <hex> <address>. A fixed script: agents only choose the values.
"""
import sys

import pypcode


def main() -> None:
    lang, data, addr = sys.argv[1], bytes.fromhex(sys.argv[2]), int(sys.argv[3], 0)
    ctx = pypcode.Context(lang)
    offset = 0
    while offset < len(data):
        try:
            res = ctx.translate(data[offset:], addr + offset, max_instructions=1)
        except Exception as e:  # noqa: BLE001 - a delay slot past the end, bad bytes: say so and stop
            print(f"{hex(addr + offset)}: stopped ({e})")
            break
        if not res.ops:
            break
        size = 0
        for op in res.ops:
            if op.opcode == pypcode.OpCode.IMARK:
                size = sum(v.size for v in op.inputs)
                for dis in ctx.disassemble(data[offset:offset + size], addr + offset, max_instructions=1).instructions:
                    print(f"{hex(dis.addr.offset)}: {dis.mnem} {dis.body}")
            else:
                print(f"    {op}")
        offset += max(size, 1)


if __name__ == "__main__":
    main()
