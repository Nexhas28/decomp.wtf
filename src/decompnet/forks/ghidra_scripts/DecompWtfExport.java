// decomp.wtf: export a structured evidence dossier for every function of the imported Program.
//
// Output (out.jsonl, one JSON object per line) keeps the reviewed exporter's {"name", "c"} shape readable by
// the previous name->C-only Python parser: every row carries "name" and "c" (a function without a C draft has
// "c": ""), so old parsers keep working. New per-function fields (status, classification, entry/space/section/
// ranges, pseudocode status + warnings, per-facet coverage and optional instruction/block/call/reference/type/
// p-code observations) are ignored by old parsers. Import-level completion metadata goes to <out>.summary.json,
// which old parsers never open.
//
// One DecompInterface is reused for the whole Program (opened once, disposed in a finally); Ghidra's API
// supports repeated function decompilation on one Program.
//
// Usage: analyzeHeadless ... -postScript DecompWtfExport.java <out.jsonl> [--instructions] [--bytes]
//        [--blocks] [--calls] [--refs] [--types] [--pcode] [--analysis-option <opt>]...
//
// Facet statuses: unknown (not requested), unavailable (not supplied here, e.g. a thunk's pseudocode),
// empty (computed, nothing present), failed (attempted, errored), partial, ok. They are distinct values.

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.framework.Application;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressRange;
import ghidra.program.model.address.AddressRangeIterator;
import ghidra.program.model.block.BasicBlockModel;
import ghidra.program.model.block.CodeBlock;
import ghidra.program.model.block.CodeBlockIterator;
import ghidra.program.model.block.CodeBlockReference;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.listing.Parameter;
import ghidra.program.model.listing.ThunkFunction;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.pcode.HighFunction;
import ghidra.program.model.pcode.PcodeOpAST;
import ghidra.program.model.pcode.Varnode;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.RefType;
import ghidra.program.model.symbol.FlowType;
import ghidra.program.model.symbol.Symbol;

import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Set;

public class DecompWtfExport extends GhidraScript {
    private static final int PCODE_LIMIT = 4096;

    private static String js(String s) {
        StringBuilder b = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': break;
                case '\t': b.append("\\t"); break;
                default:
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c)); else b.append(c);
            }
        }
        return b.append('"').toString();
    }

    private static String bytesToHex(byte[] bytes) {
        StringBuilder s = new StringBuilder(bytes.length * 2);
        for (byte x : bytes) {
            s.append(String.format("%02x", x & 0xff));
        }
        return s.toString();
    }

    /** unknown (not requested) vs unavailable (not supplied here) vs empty (computed, nothing) vs ok. */
    private static String facetStatus(boolean requested, boolean attempted, boolean hasData) {
        if (!requested) return "unknown";
        if (!attempted) return "unavailable";
        return hasData ? "ok" : "empty";
    }

    private String functionRow(DecompInterface ifc, Function f, boolean wantIns, boolean wantBytes,
            boolean wantBlocks, boolean wantCalls, boolean wantRefs, boolean wantTypes, boolean wantPcode,
            List<String> failed, List<String> rowErrors) {
        boolean external = f.isExternal();
        boolean thunk = f.isThunk();
        String classification = external ? "external" : (thunk ? "thunk" : "normal");

        Address entry = f.getEntryPoint();
        String space = "";
        long entryOff = 0;
        String section = "";
        long blockOffset = 0;
        try {
            space = entry.getAddressSpace().getName();
            entryOff = entry.getOffset();
            MemoryBlock block = currentProgram.getMemory().getBlock(entry);
            if (block != null) {
                section = block.getName();
                blockOffset = entryOff - block.getStart().getOffset();
            }
        } catch (Exception e) {
            rowErrors.add(f.getName() + ": entry/block: " + e);
        }

        // body ranges (noncontiguous bodies stay as separate extents)
        List<String> ranges = new ArrayList<>();
        try {
            AddressRangeIterator ri = f.getBody().getAddressRanges();
            while (ri.hasNext()) {
                AddressRange r = ri.next();
                ranges.add("[" + r.getMinAddress().getOffset() + "," + r.getMaxAddress().getOffset() + "]");
            }
        } catch (Exception e) {
            rowErrors.add(f.getName() + ": body ranges: " + e);
        }

        Long thunkOf = null;
        if (thunk) {
            try {
                Function target = ((ThunkFunction) f).getThunkedFunction(monitor);
                if (target != null) thunkOf = target.getEntryPoint().getOffset();
            } catch (Exception e) {
                rowErrors.add(f.getName() + ": thunk target: " + e);
            }
        }

        // pseudocode (always attempted for normal functions; thunks/externals are classified, not decompiled)
        String c = "";
        String pStatus = "unavailable";
        List<String> warnings = new ArrayList<>();
        DecompileResults results = null;
        if (external || thunk) {
            pStatus = "unavailable";
        } else {
            try {
                results = ifc.decompileFunction(f, 60, monitor);
                if (results != null && results.decompileCompleted() && results.getDecompiledFunction() != null) {
                    c = results.getDecompiledFunction().getC();
                    pStatus = "ok";
                } else {
                    pStatus = "failed";
                    String msg = results != null ? results.getErrorMessage() : "decompileFunction returned null";
                    warnings.add(msg == null || msg.isEmpty() ? "decompilation incomplete" : msg);
                    failed.add(f.getName());
                }
            } catch (Exception e) {
                pStatus = "failed";
                warnings.add(String.valueOf(e));
                failed.add(f.getName());
            }
        }
        String rowStatus = (external || thunk) ? "skipped" : (pStatus.equals("ok") ? "ok" : "failed");

        // instructions
        String insStatus = facetStatus(wantIns, !external, false);
        List<String> ins = new ArrayList<>();
        if (wantIns && !external) {
            try {
                InstructionIterator ii = currentProgram.getListing().getInstructions(f.getBody(), true);
                while (ii.hasNext()) {
                    Instruction in = ii.next();
                    StringBuilder sb = new StringBuilder("{\"address\":").append(in.getAddress().getOffset())
                            .append(",\"size\":").append(in.getLength());
                    if (wantBytes) {
                        sb.append(",\"bytes\":").append(js(bytesToHex(in.getBytes())));
                    }
                    sb.append(",\"asm\":").append(js(in.toString())).append('}');
                    ins.add(sb.toString());
                }
                insStatus = facetStatus(wantIns, true, !ins.isEmpty());
            } catch (Exception e) {
                insStatus = "failed";
                rowErrors.add(f.getName() + ": instructions: " + e);
            }
        }

        // basic blocks / CFG
        String blocksStatus = facetStatus(wantBlocks, !external, false);
        List<String> blocks = new ArrayList<>();
        if (wantBlocks && !external) {
            try {
                BasicBlockModel model = new BasicBlockModel(currentProgram);
                CodeBlockIterator it = model.getCodeBlocksContaining(f.getBody(), monitor);
                while (it.hasNext()) {
                    CodeBlock cb = it.next();
                    StringBuilder sb = new StringBuilder("{\"start\":").append(cb.getMinAddress().getOffset())
                            .append(",\"end\":").append(cb.getMaxAddress().getOffset()).append(",\"successors\":[");
                    boolean first = true;
                    for (CodeBlockReference ref : cb.getDestinations(monitor)) {
                        if (!first) sb.append(',');
                        sb.append(ref.getDestinationAddress().getOffset());
                        first = false;
                    }
                    sb.append("]}");
                    blocks.add(sb.toString());
                }
                blocksStatus = facetStatus(wantBlocks, true, !blocks.isEmpty());
            } catch (Exception e) {
                blocksStatus = "failed";
                rowErrors.add(f.getName() + ": blocks: " + e);
            }
        }

        // direct and indirect calls (computed calls may have no resolved destination)
        String callsStatus = facetStatus(wantCalls, !external, false);
        List<String> calls = new ArrayList<>();
        if (wantCalls && !external) {
            try {
                InstructionIterator ii = currentProgram.getListing().getInstructions(f.getBody(), true);
                while (ii.hasNext()) {
                    Instruction in = ii.next();
                    FlowType ft = in.getFlowType();
                    if (!ft.isCall() && !ft.isComputed()) continue;
                    long addr = in.getAddress().getOffset();
                    boolean indirect = ft.isComputed();
                    Long target = null;
                    String tname = "";
                    for (Reference ref : in.getReferencesFrom()) {
                        if (ref.getReferenceType().isCall()) {
                            target = ref.getToAddress().getOffset();
                            Symbol sym = currentProgram.getSymbolTable().getPrimarySymbol(ref.getToAddress());
                            if (sym != null) tname = sym.getName();
                            break;
                        }
                    }
                    StringBuilder sb = new StringBuilder("{\"address\":").append(addr)
                            .append(",\"target\":").append(target == null ? "null" : target)
                            .append(",\"name\":").append(js(tname))
                            .append(",\"indirect\":").append(indirect).append('}');
                    calls.add(sb.toString());
                }
                callsStatus = facetStatus(wantCalls, true, !calls.isEmpty());
            } catch (Exception e) {
                callsStatus = "failed";
                rowErrors.add(f.getName() + ": calls: " + e);
            }
        }

        // data / string / literal references
        String refsStatus = facetStatus(wantRefs, !external, false);
        List<String> refs = new ArrayList<>();
        if (wantRefs && !external) {
            try {
                InstructionIterator ii = currentProgram.getListing().getInstructions(f.getBody(), true);
                while (ii.hasNext()) {
                    Instruction in = ii.next();
                    for (Reference ref : in.getReferencesFrom()) {
                        RefType rt = ref.getReferenceType();
                        if (rt.isCall()) continue;
                        Address to = ref.getToAddress();
                        String kind = "data";
                        String value = "";
                        Data d = currentProgram.getListing().getDefinedDataAt(to);
                        if (d != null) {
                            try {
                                Object v = d.getValue();
                                if (v instanceof String) kind = "string";
                                value = String.valueOf(v);
                            } catch (Exception e) {
                                value = d.getDataType().getName();
                            }
                        }
                        StringBuilder sb = new StringBuilder("{\"address\":").append(in.getAddress().getOffset())
                                .append(",\"kind\":").append(js(kind))
                                .append(",\"target\":").append(to.getOffset())
                                .append(",\"value\":").append(js(value)).append('}');
                        refs.add(sb.toString());
                    }
                }
                refsStatus = facetStatus(wantRefs, true, !refs.isEmpty());
            } catch (Exception e) {
                refsStatus = "failed";
                rowErrors.add(f.getName() + ": references: " + e);
            }
        }

        // types present in the analysis database. origin "decompiler" = recovered, not original source truth.
        String typesStatus = facetStatus(wantTypes, !external, false);
        List<String> types = new ArrayList<>();
        if (wantTypes) {
            try {
                types.add("{\"name\":" + js(f.getName()) + ",\"signature\":"
                        + js(f.getSignature().getPrototypeString()) + ",\"origin\":\"decompiler\",\"complete\":false}");
                for (Parameter p : f.getParameters()) {
                    types.add("{\"name\":" + js(p.getName()) + ",\"signature\":" + js(p.getDataType().getName())
                            + ",\"origin\":\"decompiler\",\"complete\":false}");
                }
                typesStatus = facetStatus(wantTypes, true, !types.isEmpty());
            } catch (Exception e) {
                typesStatus = "failed";
                rowErrors.add(f.getName() + ": types: " + e);
            }
        }

        // bounded p-code for targeted investigations
        String pcodeStatus = "unknown";
        StringBuilder pcode = new StringBuilder();
        if (wantPcode) {
            if (!external && !thunk && results != null) {
                try {
                    HighFunction hf = results.getHighFunction();
                    if (hf != null) {
                        Iterator<PcodeOpAST> ops = hf.getPcodeOps();
                        List<String> outOps = new ArrayList<>();
                        int n = 0;
                        while (ops.hasNext() && n < PCODE_LIMIT) {
                            PcodeOpAST op = ops.next();
                            StringBuilder sb = new StringBuilder("{\"address\":")
                                    .append(op.getSeqnum().getTarget().getOffset())
                                    .append(",\"mnemonic\":").append(js(op.getMnemonic()));
                            Varnode output = op.getOutput();
                            sb.append(",\"output\":").append(output != null && output.getAddress() != null
                                    ? output.getAddress().getOffset() : "null");
                            sb.append(",\"inputs\":[");
                            Varnode[] inputs = op.getInputs();
                            for (int i = 0; i < inputs.length; i++) {
                                if (i > 0) sb.append(',');
                                Varnode v = inputs[i];
                                sb.append(v != null && v.getAddress() != null ? v.getAddress().getOffset() : "null");
                            }
                            sb.append("]}");
                            outOps.add(sb.toString());
                            n++;
                        }
                        boolean truncated = ops.hasNext();
                        pcode.append("{\"status\":").append(js(truncated ? "partial" : "ok"))
                                .append(",\"limit\":").append(PCODE_LIMIT)
                                .append(",\"truncated\":").append(truncated)
                                .append(",\"ops\":[").append(String.join(",", outOps)).append("]}");
                        pcodeStatus = truncated ? "partial" : (outOps.isEmpty() ? "empty" : "ok");
                    } else {
                        pcode.append("{\"status\":\"unavailable\"}");
                        pcodeStatus = "unavailable";
                    }
                } catch (Exception e) {
                    pcode.append("{\"status\":\"failed\"}");
                    pcodeStatus = "failed";
                    rowErrors.add(f.getName() + ": p-code: " + e);
                }
            } else {
                pcode.append("{\"status\":\"unavailable\"}");
                pcodeStatus = "unavailable";
            }
        }

        StringBuilder row = new StringBuilder("{\"name\":").append(js(f.getName()))
                .append(",\"c\":").append(js(c))
                .append(",\"status\":").append(js(rowStatus))
                .append(",\"classification\":").append(js(classification))
                .append(",\"namespace\":").append(js(f.getParentNamespace() != null
                        ? f.getParentNamespace().getName() : ""))
                .append(",\"entry\":").append(entryOff)
                .append(",\"space\":").append(js(space))
                .append(",\"section\":").append(js(section))
                .append(",\"block_offset\":").append(blockOffset)
                .append(",\"ranges\":[").append(String.join(",", ranges)).append(']')
                .append(",\"thunk_of\":").append(thunkOf == null ? "null" : thunkOf)
                .append(",\"pseudocode\":{\"status\":").append(js(pStatus))
                .append(",\"warnings\":[");
        for (int i = 0; i < warnings.size(); i++) {
            if (i > 0) row.append(',');
            row.append(js(warnings.get(i)));
        }
        row.append("]}")
                .append(",\"coverage\":{\"pseudocode\":").append(js(pStatus))
                .append(",\"instructions\":").append(js(insStatus))
                .append(",\"blocks\":").append(js(blocksStatus))
                .append(",\"calls\":").append(js(callsStatus))
                .append(",\"references\":").append(js(refsStatus))
                .append(",\"types\":").append(js(typesStatus))
                .append(",\"pcode\":").append(js(pcodeStatus)).append('}');
        if (wantIns) row.append(",\"instructions\":[").append(String.join(",", ins)).append(']');
        if (wantBlocks) row.append(",\"blocks\":[").append(String.join(",", blocks)).append(']');
        if (wantCalls) row.append(",\"calls\":[").append(String.join(",", calls)).append(']');
        if (wantRefs) row.append(",\"refs\":[").append(String.join(",", refs)).append(']');
        if (wantTypes) row.append(",\"types\":[").append(String.join(",", types)).append(']');
        if (wantPcode) row.append(",\"pcode\":").append(pcode);
        return row.append('}').toString();
    }

    private void writeSummary(String outPath, String status, int total, int exported, List<String> failed,
            List<String> rowErrors, String[] args) {
        try (PrintWriter w = new PrintWriter(new OutputStreamWriter(
                new FileOutputStream(outPath + ".summary.json"), StandardCharsets.UTF_8))) {
            StringBuilder sb = new StringBuilder("{\"type\":\"summary\",\"status\":").append(js(status))
                    .append(",\"functions_total\":").append(total)
                    .append(",\"functions_exported\":").append(exported)
                    .append(",\"failed\":[");
            for (int i = 0; i < failed.size(); i++) {
                if (i > 0) sb.append(',');
                sb.append(js(failed.get(i)));
            }
            sb.append("],\"row_errors\":[");
            for (int i = 0; i < rowErrors.size(); i++) {
                if (i > 0) sb.append(',');
                sb.append(js(rowErrors.get(i)));
            }
            sb.append("],\"language\":").append(js(currentProgram.getLanguageID().getIdAsString()))
                    .append(",\"cspec\":").append(js(currentProgram.getCompilerSpec().getCompilerSpecID()
                            .getIdAsString()))
                    .append(",\"provider\":\"ghidra-headless\",\"version\":")
                    .append(js(Application.getApplicationVersion()))
                    .append(",\"args\":[");
            for (int i = 0; i < args.length; i++) {
                if (i > 0) sb.append(',');
                sb.append(js(args[i]));
            }
            sb.append("]}");
            w.println(sb.toString());
        } catch (Exception e) {
            println("DecompWtfExport: could not write summary: " + e);
        }
    }

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 1) {
            println("Usage: DecompWtfExport.java <out.jsonl> [--instructions] [--bytes] [--blocks] [--calls]"
                    + " [--refs] [--types] [--pcode] [--analysis-option <opt>]...");
            return;
        }
        String outPath = args[0];
        Set<String> flags = new HashSet<>(Arrays.asList(args).subList(1, args.length));
        boolean wantIns = flags.contains("--instructions");
        boolean wantBytes = flags.contains("--bytes");
        boolean wantBlocks = flags.contains("--blocks");
        boolean wantCalls = flags.contains("--calls");
        boolean wantRefs = flags.contains("--refs");
        boolean wantTypes = flags.contains("--types");
        boolean wantPcode = flags.contains("--pcode");

        int total = currentProgram.getFunctionManager().getFunctionCount();
        int exported = 0;
        boolean cancelled = false;
        List<String> failed = new ArrayList<>();
        List<String> rowErrors = new ArrayList<>();

        DecompInterface ifc = new DecompInterface();
        ifc.openProgram(currentProgram);
        PrintWriter w = new PrintWriter(new OutputStreamWriter(
                new FileOutputStream(outPath), StandardCharsets.UTF_8));
        try {
            FunctionIterator it = currentProgram.getFunctionManager().getFunctions(true);
            while (it.hasNext() && !monitor.isCancelled()) {
                Function f = it.next();
                exported++;
                w.println(functionRow(ifc, f, wantIns, wantBytes, wantBlocks, wantCalls, wantRefs, wantTypes,
                        wantPcode, failed, rowErrors));
            }
            if (monitor.isCancelled()) cancelled = true;
            String status = cancelled ? "cancelled"
                    : (failed.isEmpty() && rowErrors.isEmpty() ? "complete" : "partial");
            writeSummary(outPath, status, total, exported, failed, rowErrors, args);
        } finally {
            w.close();
            ifc.dispose();  // one DecompInterface per owned Program, disposed reliably
        }
    }
}
