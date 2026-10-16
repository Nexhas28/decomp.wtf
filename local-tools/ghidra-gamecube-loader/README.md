# ghidra-gamecube-loader

Ghidra extension (Apache-2.0, https://github.com/Cuyler36/Ghidra-GameCube-Loader) that loads GameCube/Wii DOL executables, REL/RSO modules, apploaders and RAM dumps. It maps the DOL sections, adds the GC/Wii hardware memory blocks and uses the Gekko/Broadway PowerPC language (with paired-single instructions). Useful for decomp.wtf GameCube/Wii projects that want a correctly laid out Ghidra database, and it can load CodeWarrior .map symbol files.

Run: `bash local-tools/ghidra-gamecube-loader/test.sh`. For real use, install `GameCubeLoader-1.3.0-921504c-Ghidra_12.1.zip` through Ghidra's File > Install Extensions, then import a .dol/.rel and pick "Nintendo GameCube/Wii Binary".

Installed: prebuilt release asset of tag 1.3.1 (commit 921504c, built for Ghidra 12.1; the asset file name still says 1.3.0) downloaded with `gh release download 1.3.1`. sha256 9892f28fc1e7f19bb3fcec7cfb4ad2ab7504503b571d93de8b142a6f7bec8ed0. Needs Java 21 and Ghidra 12.1.4. The zip ships the Gekko/Broadway .slaspec only; Ghidra compiles it on first load (a few seconds, many harmless sleigh warnings).

Headless note: pass `-loader-autoloadMaps false` unless a .map file sits next to the DOL; otherwise the loader opens a "Load Symbols?" dialog and the import fails with HeadlessException.

Test result: PASS (2026-10-16). test.sh hand-builds a tiny DOL, imports it with analyzeHeadless using a throwaway user.home and checks the language, the .text/.data memory blocks and that li, addi and ps_add disassemble.
