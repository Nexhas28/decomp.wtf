"""BIOS calls: functions that are nothing but a jump into the console's BIOS or kernel (a "stub").

A game calls the console's built-in system code through tiny functions its SDK wrote in assembly. They hold no game
code, and no C compiles to them (C can't put the call number in t1, or emit syscall/swi), so they're set aside for
agents the way hand-written assembly is (handwritten.py), with the call named so people see what it is.

Only exact stub shapes count, every instruction accounted for: a function that also does work of its own is left alone,
since game code taken for a stub would never be worked on.

PlayStation: the BIOS's A0, B0 and C0 tables, `li t2, 0xA0; jr t2; li t1, n` (t1 set before the jump or in its
delay slot); and kernel calls, `li a0, n; syscall; jr ra; nop`. The splitter sometimes runs several stubs together
(they sit back to back): all of them still have to be stubs.
PlayStation 2: EE kernel calls, `li v1, n; syscall; jr ra; nop` (n negative for the interrupt-context i... calls).
Game Boy Advance, Nintendo DS: `swi n; bx lr`, thumb or ARM.
Game Boy / Game Boy Color: games call no BIOS after the boot ROM hands over, so nothing here. N64, GameCube, Wii: no
BIOS either; their SDK libraries (libultra, the Dolphin SDK) are compiled C an agent can match, so they stay.
"""

from __future__ import annotations

# PS1 BIOS functions by table and number (nocash's psx-spx names). Numbers not here are shown bare.
PSX_A0 = {
    0x00: "open", 0x01: "lseek", 0x02: "read", 0x03: "write", 0x04: "close", 0x05: "ioctl", 0x06: "exit",
    0x08: "getc", 0x09: "putc", 0x0A: "todigit", 0x0C: "strtoul", 0x0D: "strtol", 0x0E: "abs", 0x0F: "labs",
    0x10: "atoi", 0x11: "atol", 0x12: "atob", 0x13: "setjmp", 0x14: "longjmp", 0x15: "strcat", 0x16: "strncat",
    0x17: "strcmp", 0x18: "strncmp", 0x19: "strcpy", 0x1A: "strncpy", 0x1B: "strlen", 0x1C: "index",
    0x1D: "rindex", 0x1E: "strchr", 0x1F: "strrchr", 0x20: "strpbrk", 0x21: "strspn", 0x22: "strcspn",
    0x23: "strtok", 0x24: "strstr", 0x25: "toupper", 0x26: "tolower", 0x27: "bcopy", 0x28: "bzero", 0x29: "bcmp",
    0x2A: "memcpy", 0x2B: "memset", 0x2C: "memmove", 0x2D: "memcmp", 0x2E: "memchr", 0x2F: "rand", 0x30: "srand",
    0x31: "qsort", 0x33: "malloc", 0x34: "free", 0x35: "lsearch", 0x36: "bsearch", 0x37: "calloc",
    0x38: "realloc", 0x39: "InitHeap", 0x3A: "_exit", 0x3B: "getchar", 0x3C: "putchar", 0x3D: "gets", 0x3E: "puts",
    0x3F: "printf", 0x41: "LoadTest", 0x42: "Load", 0x43: "Exec", 0x44: "FlushCache", 0x49: "GPU_cw",
    0x4E: "GPU_sync", 0x51: "LoadExec", 0x54: "_96_init", 0x55: "_bu_init", 0x56: "_96_remove", 0x70: "_bu_init",
    0x71: "_96_init", 0x72: "_96_remove", 0x96: "AddCDROMDevice", 0x97: "AddMemCardDevice", 0x9C: "SetConf",
    0x9D: "GetConf", 0x9F: "SetMem", 0xA0: "_boot", 0xA1: "SystemError", 0xA2: "EnqueueCdIntr",
    0xA3: "DequeueCdIntr", 0xA4: "CdGetLbn", 0xA5: "CdReadSector", 0xA6: "CdGetStatus", 0xAB: "_card_info",
    0xAC: "_card_load", 0xAD: "_card_auto", 0xAF: "_card_clear", 0xB4: "GetSystemInfo",
}
PSX_B0 = {
    0x00: "SysMalloc", 0x02: "SetRCnt", 0x03: "GetRCnt", 0x04: "StartRCnt", 0x05: "StopRCnt", 0x06: "ResetRCnt",
    0x07: "DeliverEvent", 0x08: "OpenEvent", 0x09: "CloseEvent", 0x0A: "WaitEvent", 0x0B: "TestEvent",
    0x0C: "EnableEvent", 0x0D: "DisableEvent", 0x0E: "OpenTh", 0x0F: "CloseTh", 0x10: "ChangeTh", 0x12: "InitPAD",
    0x13: "StartPAD", 0x14: "StopPAD", 0x15: "PAD_init", 0x16: "PAD_dr", 0x17: "ReturnFromException",
    0x18: "ResetEntryInt", 0x19: "HookEntryInt", 0x20: "UnDeliverEvent", 0x32: "open", 0x33: "lseek", 0x34: "read",
    0x35: "write", 0x36: "close", 0x37: "ioctl", 0x38: "exit", 0x3A: "getc", 0x3B: "putc", 0x3C: "getchar",
    0x3D: "putchar", 0x3E: "gets", 0x3F: "puts", 0x40: "cd", 0x41: "format", 0x42: "firstfile", 0x43: "nextfile",
    0x44: "rename", 0x45: "delete", 0x46: "undelete", 0x47: "AddDevice", 0x48: "DelDevice", 0x49: "PrintInstalledDevices",
    0x4A: "InitCARD", 0x4B: "StartCARD", 0x4C: "StopCARD", 0x4E: "_card_write", 0x4F: "_card_read",
    0x50: "_new_card", 0x51: "Krom2RawAdd", 0x54: "_get_errno", 0x55: "_get_error", 0x56: "GetC0Table",
    0x57: "GetB0Table", 0x58: "_card_chan", 0x5B: "ChangeClearPad", 0x5C: "_card_status", 0x5D: "_card_wait",
}
PSX_C0 = {
    0x00: "InitRCnt", 0x01: "InitException", 0x02: "SysEnqIntRP", 0x03: "SysDeqIntRP", 0x04: "get_free_EvCB_slot",
    0x05: "get_free_TCB_slot", 0x06: "ExceptionHandler", 0x07: "InstallExceptionHandlers", 0x08: "SysInitMemory",
    0x09: "SysInitKMem", 0x0A: "ChangeClearRCnt", 0x0C: "InitDefInt", 0x0D: "SetIrqAutoAck", 0x12: "InstallDevices",
    0x13: "FlushStdInOutPut", 0x1C: "AdjustA0Table",
}
_PSX_TABLES = {0xA0: PSX_A0, 0xB0: PSX_B0, 0xC0: PSX_C0}
PSX_SYSCALLS = {0: "NoFunction", 1: "EnterCriticalSection", 2: "ExitCriticalSection", 3: "ChangeThreadSubFunction"}

# PS2 EE kernel calls by number (the interrupt-context i... versions are called with negative numbers).
PS2_SYSCALLS = {
    0x01: "ResetEE", 0x02: "SetGsCrt", 0x04: "Exit", 0x06: "LoadExecPS2", 0x07: "ExecPS2", 0x10: "AddIntcHandler",
    0x11: "RemoveIntcHandler", 0x12: "AddDmacHandler", 0x13: "RemoveDmacHandler", 0x14: "_EnableIntc",
    0x15: "_DisableIntc", 0x16: "_EnableDmac", 0x17: "_DisableDmac", 0x20: "CreateThread", 0x21: "DeleteThread",
    0x22: "StartThread", 0x23: "ExitThread", 0x24: "ExitDeleteThread", 0x25: "TerminateThread",
    0x29: "ChangeThreadPriority", 0x2B: "RotateThreadReadyQueue", 0x2D: "ReleaseWaitThread", 0x2F: "GetThreadId",
    0x30: "ReferThreadStatus", 0x32: "SleepThread", 0x33: "WakeupThread", 0x35: "CancelWakeupThread",
    0x37: "SuspendThread", 0x39: "ResumeThread", 0x3C: "SetupThread", 0x3D: "SetupHeap", 0x3E: "EndOfHeap",
    0x40: "CreateSema", 0x41: "DeleteSema", 0x42: "SignalSema", 0x44: "WaitSema", 0x45: "PollSema",
    0x47: "ReferSemaStatus", 0x64: "FlushCache", 0x70: "GsGetIMR", 0x71: "GsPutIMR", 0x73: "SetVSyncFlag",
    0x74: "SetSyscall", 0x76: "SifDmaStat", 0x77: "SifSetDma", 0x78: "SifSetDChain", 0x79: "SifSetReg",
    0x7A: "SifGetReg", 0x7C: "Deci2Call", 0x7F: "GetMemorySize",
}

GBA_SWIS = {
    0x00: "SoftReset", 0x01: "RegisterRamReset", 0x02: "Halt", 0x03: "Stop", 0x04: "IntrWait", 0x05: "VBlankIntrWait",
    0x06: "Div", 0x07: "DivArm", 0x08: "Sqrt", 0x09: "ArcTan", 0x0A: "ArcTan2", 0x0B: "CpuSet", 0x0C: "CpuFastSet",
    0x0D: "GetBiosChecksum", 0x0E: "BgAffineSet", 0x0F: "ObjAffineSet", 0x10: "BitUnPack", 0x11: "LZ77UnCompWram",
    0x12: "LZ77UnCompVram", 0x13: "HuffUnComp", 0x14: "RLUnCompWram", 0x15: "RLUnCompVram",
    0x16: "Diff8bitUnFilterWram", 0x17: "Diff8bitUnFilterVram", 0x18: "Diff16bitUnFilter", 0x19: "SoundBias",
    0x1A: "SoundDriverInit", 0x1B: "SoundDriverMode", 0x1C: "SoundDriverMain", 0x1D: "SoundDriverVSync",
    0x1E: "SoundChannelClear", 0x1F: "MidiKey2Freq", 0x25: "MultiBoot", 0x26: "HardReset", 0x27: "CustomHalt",
    0x28: "SoundDriverVSyncOff", 0x29: "SoundDriverVSyncOn", 0x2A: "SoundGetJumpList",
}
NDS_SWIS = {
    0x00: "SoftReset", 0x03: "WaitByLoop", 0x04: "IntrWait", 0x05: "VBlankIntrWait", 0x06: "Halt", 0x07: "Stop",
    0x08: "SoundBias", 0x09: "Div", 0x0B: "CpuSet", 0x0C: "CpuFastSet", 0x0D: "Sqrt", 0x0E: "GetCRC16",
    0x0F: "IsDebugger", 0x10: "BitUnPack", 0x11: "LZ77UnCompWram", 0x12: "LZ77UnCompVram", 0x13: "HuffUnComp",
    0x14: "RLUnCompWram", 0x15: "RLUnCompVram", 0x16: "Diff8bitUnFilterWram", 0x18: "Diff16bitUnFilter",
}

_PLATFORM_NAMES = {"psx": "PS1", "ps": "PS1", "ps2": "PS2", "gba": "GBA", "nds": "DS"}

_NOP, _JR_RA, _JR_T2 = 0x00000000, 0x03E00008, 0x01400008
_T1, _T2, _A0, _V1 = 9, 10, 4, 3


def _words(code: bytes, order: str) -> list[int]:
    words = [int.from_bytes(code[at:at + 4], order) for at in range(0, len(code) - 3, 4)]
    while words and words[-1] == _NOP:  # alignment padding
        words.pop()
    return words


def _li(w: int) -> tuple[int, int] | None:
    """(register, value) for `li rt, imm` (addiu rt, zero, imm or ori rt, zero, imm), else None."""
    op, rs, rt = w >> 26, (w >> 21) & 31, (w >> 16) & 31
    if rs != 0 or rt == 0 or op not in (0x09, 0x0D):
        return None
    imm = w & 0xFFFF
    return rt, (imm - 0x10000 if op == 0x09 and imm & 0x8000 else imm)


def _syscall(w: int) -> bool:
    return w & 0xFC00003F == 0x0000000C


def _psx_table_stubs(words: list[int]) -> list[str] | None:
    """The calls a run of A0/B0/C0 stubs makes, or None when anything else is there."""
    calls: list[str] = []
    t1 = t2 = None
    i = 0
    while i < len(words):
        w = words[i]
        if w == _NOP:
            i += 1
            continue
        if (li := _li(w)) is not None and li[0] in (_T1, _T2):
            if li[0] == _T1:
                t1 = li[1]
            else:
                t2 = li[1]
            i += 1
            continue
        if w != _JR_T2:
            return None
        if i + 1 < len(words) and (slot := _li(words[i + 1])) is not None and slot[0] == _T1:
            t1 = slot[1]  # the delay slot runs before the jump lands
            i += 1
        elif i + 1 < len(words) and words[i + 1] != _NOP:
            return None
        if t2 not in _PSX_TABLES or t1 is None or not 0 <= t1 <= 0xFF:
            return None
        name = _PSX_TABLES[t2].get(t1)
        calls.append(f"{t2:02X}:{t1:02X}" + (f" ({name})" if name else ""))
        t1 = t2 = None
        i += 1
    return calls if calls and t1 is None and t2 is None else None


def _syscall_stub(words: list[int], reg: int) -> int | None:
    """n for `li reg, n; syscall; jr ra; nop` (any order of the li before the syscall, nops allowed), else None."""
    n = None
    seen_syscall = seen_return = False
    for i, w in enumerate(words):
        if w == _NOP:
            continue
        if (li := _li(w)) is not None and li[0] == reg and not seen_syscall and n is None:
            n = li[1]
        elif _syscall(w) and not seen_syscall and n is not None:
            seen_syscall = True
        elif w == _JR_RA and seen_syscall and not seen_return:
            seen_return = True
            if i + 1 < len(words) and words[i + 1] != _NOP:  # a delay slot doing work
                return None
        else:
            return None
    return n if seen_syscall and seen_return else None


def _swi(code: bytes) -> int | None:
    """n for `swi n; bx lr` (thumb, then ARM), trailing padding allowed, else None."""
    halves = [int.from_bytes(code[at:at + 2], "little") for at in range(0, len(code) - 1, 2)]
    while halves and halves[-1] in (0x0000, 0x46C0):  # padding, nop (mov r8, r8)
        halves.pop()
    if len(halves) == 2 and halves[0] >> 8 == 0xDF and halves[1] == 0x4770:
        return halves[0] & 0xFF
    words = [int.from_bytes(code[at:at + 4], "little") for at in range(0, len(code) - 3, 4)]
    while words and words[-1] in (0, 0xE1A00000):  # padding, nop (mov r0, r0)
        words.pop()
    if len(words) == 2 and words[0] >> 24 == 0xEF and words[1] == 0xE12FFF1E:
        return (words[0] >> 16) & 0xFF
    return None


def reason(code: bytes, platform: str, big_endian: bool = False) -> str | None:
    """Why this function (its code, from the entry) is a BIOS call and not game code, or None."""
    platform = (platform or "").lower()
    label = _PLATFORM_NAMES.get(platform)
    if not code or label is None:
        return None
    tail = "there's no game code in it to decompile"
    if platform in ("psx", "ps", "ps2"):
        words = _words(code, "big" if big_endian else "little")
        if platform != "ps2" and (calls := _psx_table_stubs(words)):
            what = f"{label} BIOS call {calls[0]} wrapper" if len(calls) == 1 else \
                f"{len(calls)} {label} BIOS call wrappers run together: {', '.join(calls)}"
            return f"{what}: {tail}"
        reg = _V1 if platform == "ps2" else _A0
        if (n := _syscall_stub(words, reg)) is not None:
            names = PS2_SYSCALLS if platform == "ps2" else PSX_SYSCALLS
            name = "an interrupt-context call" if n < 0 and platform == "ps2" else names.get(n)
            num = f"-0x{-n:02X}" if n < 0 else f"0x{n:02X}"
            return f"{label} kernel call {num}" + (f" ({name})" if name else "") + f" wrapper (syscall): {tail}"
        return None
    if (n := _swi(code)) is not None:
        name = (GBA_SWIS if platform == "gba" else NDS_SWIS).get(n)
        return f"{label} BIOS call swi 0x{n:02X}" + (f" ({name})" if name else "") + f" wrapper: {tail}"
    return None
