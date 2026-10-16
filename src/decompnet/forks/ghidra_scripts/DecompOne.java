// Decompile one ROM function by address/size and dump its C, calls and data refs.
// Used by the ROM-evidence lane (forks/ghidra.py decompile_rom_function): the import runs with
// -noanalysis, so this script does its own disassembly, data definition and function creation.
//@category decomp.wtf
//@keybinding
//@menupath
//@toolbar

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.RefType;
import ghidra.program.model.symbol.SourceType;

public class DecompOne extends GhidraScript {

    @Override
    public void run() throws Exception {
        long addr = Long.decode(getScriptArgs()[0]);
        long size = Long.parseLong(getScriptArgs()[1]);
        Address a = toAddr(addr);
        Address end = a.add(size - 1);

        // force the whole body to code, instruction by instruction (fixed 4-byte MIPS here)
        currentProgram.getListing().clearCodeUnits(a, end, false);
        Address cur = a;
        while (cur != null && cur.compareTo(end) <= 0) {
            if (getInstructionAt(cur) == null && !disassemble(cur)) {
                println("DISASSEMBLE FAILED at " + cur);
                break;
            }
            cur = cur.add(4);
        }

        Function f = getFunctionAt(a);
        if (f == null) {
            currentProgram.getFunctionManager().createFunction(
                    "func_" + Long.toHexString(addr), a, new AddressSet(a, end),
                    SourceType.USER_DEFINED);
            f = getFunctionAt(a);
        }
        if (f == null) {
            println("NO FUNCTION AT " + a);
            return;
        }

        DecompInterface di = new DecompInterface();
        try {
            di.openProgram(currentProgram);
            DecompileResults r = di.decompileFunction(f, 90, monitor);
            if (r != null && r.decompileCompleted()) {
                println("=== DECOMPILED C ===");
                println(r.getDecompiledFunction().getC());
            } else {
                println("=== DECOMPILE FAILED: " + (r == null ? "null" : r.getErrorMessage()));
            }
            ghidra.program.model.listing.InstructionIterator it =
                    currentProgram.getListing().getInstructions(f.getBody(), true);
            println("=== CALLS ===");
            while (it.hasNext()) {
                Instruction inst = it.next();
                for (Reference ref : inst.getReferencesFrom()) {
                    if (ref.getReferenceType().isCall()) {
                        println(inst.getAddress() + " " + inst + "  ->  " + ref.getToAddress());
                    }
                }
            }
            println("=== DATA REFS ===");
            it = currentProgram.getListing().getInstructions(f.getBody(), true);
            while (it.hasNext()) {
                Instruction inst = it.next();
                for (Reference ref : inst.getReferencesFrom()) {
                    RefType t = ref.getReferenceType();
                    if (t.isData() || t.isRead() || t.isWrite()) {
                        Data d = getDataAt(ref.getToAddress());
                        println(inst.getAddress() + " " + inst + "  ->  " + ref.getToAddress()
                                + (d != null && d.hasStringValue()
                                   ? "  \"" + d.getDefaultValueRepresentation() + "\"" : ""));
                    }
                }
            }
        } finally {
            di.dispose();
        }
    }
}
