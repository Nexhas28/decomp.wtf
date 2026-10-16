# ghidra-delinker

boricj/ghidra-delinker-extension: a Ghidra extension that "delinks" parts of a program back into relocatable object files (ELF/COFF/OMF; x86, x86-64, MIPS) with a rebuilt symbol and relocation table.
Useful for decomp.wtf because a finished/analysed function range can be exported as a linkable .o, which is the basis of Ship-of-Theseus style matching decomps and of splitting a binary into per-file objects.

Run: `bash local-tools/ghidra-delinker/test.sh`. Manually, install the zip in Ghidra (File > Install Extensions) or unzip it into a Ghidra user Extensions dir, then headless:
`analyzeHeadless <proj> p -import prog.elf -scriptPath <ext>/ghidra-delinker-extension/ghidra_scripts -postScript DelinkProgram.java /exporter "ELF relocatable object" /include foo /export out/foo.o`

Installed: release v0.9.2 asset `ghidra_12.1.4_PUBLIC_20260926_ghidra-delinker-extension.zip` (prebuilt, kept in this folder; sha256 in tool.json). The Ghidra install is not modified: the test unzips into a throwaway user.home.
Needs Ghidra 12.1.4 + JDK 21 (/opt/homebrew/opt/openjdk@21), plus clang, lld@21 and llvm@21 (Homebrew) for the test.
Test result: PASS. test.sh builds a tiny x86-64 ELF, runs it through analyzeHeadless, delinks `foo` and checks the output is a relocatable x86-64 ELF with symbols foo (T), bar and counter (U) and R_X86_64_PC32 relocations against bar and counter.
