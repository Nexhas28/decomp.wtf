# ghidra-n64-loader

Ghidra loader extension (https://github.com/zeroKilo/N64LoaderWV, no license file in the repo) for Nintendo 64 ROMs (.z64/.n64/.v64). It fixes the ROM byte order, maps the N64 memory map (RDRAM, ROM at 0xB0000000, boot code, PIF, SP/DP/MI/VI/AI/PI/RI/SI registers) and labels the hardware registers. It can also take an RDRAM dump, an N64sym file or a byte-pattern file to seed function names. Useful for decomp.wtf N64 projects that want to explore a ROM in Ghidra before or while matching it.

Run: `bash local-tools/ghidra-n64-loader/test.sh`. For real use, install `ghidra_12.1.4_PUBLIC_20261016_N64LoaderWV.zip` through Ghidra's File > Install Extensions, then import a ROM and pick "N64 Loader by Warranty Voider". Headless options: `-loader-rdram`, `-loader-signature`, `-loader-pif`, see the upstream README.

Install: repo master at commit 2bd5376 (tag 12.1.3, built for Ghidra 12.1.3 upstream), rebuilt unmodified from source with Gradle 8.11.1 and Java 21 against Ghidra 12.1.4 (`gradle buildExtension`). The built zip is kept in this folder (sha256 in tool.json).

Test result: pass. The test installs the zip into a throwaway Ghidra user dir, builds a tiny ROM-shaped file by hand (big-endian .z64 and a byte-swapped .v64, no game data), imports each with analyzeHeadless and `-loader N64LoaderWVLoader`, and checks for language MIPS:BE:64:64-32addr, the .rom/.ram/.vireg blocks, the VI_STATUS label, and that the code at 0x80000400 reads back as `addiu sp,sp,-0x8` for both byte orders. Needs Java 21 and Ghidra 12.1.4.
