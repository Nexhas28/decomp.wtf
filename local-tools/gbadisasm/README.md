# gbadisasm

A small disassembler for Game Boy Advance ROMs (ARM and Thumb code), by jiangzhengwenjz. It takes a `.gba` ROM plus an optional config file that marks function, label, jump table and pool locations, and prints assembly.

Useful for decomp.wtf because it produces GBA assembly that can be fed into the byte-matching workflow (asm-differ / m2c). Its config file lets you mark functions and labels so the output is easier to match.

Run it: `local-tools/gbadisasm/bin/gbadisasm rom.gba -c rom.cfg > out.s`
Options: `-c` config file, `-l` link address (default 0x8000000), `-s` ROM has no header.

Install: built from source at commit 940def2c91d678ee671716b8b44a2223c0ff2c62 (default branch `new_features`, last pushed 2023-07-15) of https://github.com/jiangzhengwenjz/gbadisasm. The repo ships its own Capstone 3.0.5-rc2 tarball, which the Makefile builds statically (ARM only). Only the `gbadisasm` binary is kept here (arm64, Mach-O).

Test: `bash local-tools/gbadisasm/test.sh` assembles a tiny ARM+Thumb ROM with the Homebrew arm-none-eabi toolchain, disassembles it, and greps for the expected output lines. Last run: PASS.
