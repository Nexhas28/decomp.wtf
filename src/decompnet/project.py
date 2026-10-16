"""Project definitions (project.toml) and importing target functions into the network."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import time
import tomllib
from pathlib import Path

from . import scoring, seeds, toolchains
from .compiler import CompilerConfig, compile_source
from .elfobj import extract_function, function_locations, list_functions
from .store import Store, hex_entry


# A project may only be imported with a declared lawful basis for sharing its code.
LEGAL_BASES = {
    "own-code": "written by the person or team importing it",
    "open-source": "released by its authors under an open license",
    "homebrew": "homebrew whose author allows decompilation and sharing",
    "permission": "imported with written permission from the rights holder",
}


class ProjectRejected(ValueError):
    pass


def check_legal(cfg: dict) -> dict:
    """Refuse projects without a lawful basis. This is policy, not paperwork: the network only
    hosts code that may legally be shared (no commercial games without the rights holder's OK)."""
    legal = cfg.get("legal") or {}
    basis = legal.get("basis")
    if basis not in LEGAL_BASES:
        raise ProjectRejected(
            "project.toml needs a [legal] section with basis = one of " + ", ".join(LEGAL_BASES)
            + ". Commercial games can only be imported with the rights holder's permission.")
    if basis != "own-code" and not str(legal.get("evidence", "")).strip():
        raise ProjectRejected(f"basis '{basis}' needs legal.evidence: a link to the license, "
                              "the author's statement, or a reference to the written permission")
    if not str(legal.get("license", "")).strip():
        raise ProjectRejected("legal.license must name the license contributions are shared under (e.g. CC0-1.0)")
    return legal


def load_config(root: Path) -> dict:
    with open(root / "project.toml", "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------- configuration schema (plan 18.9)

class ConfigError(ProjectRejected):
    """A governed config block holds an unknown key, a value of the wrong type or an unsafe value."""


def _bool(v):
    return None if isinstance(v, bool) else "must be true or false"


def _str(v):
    return None if isinstance(v, str) and v.strip() else "must be a non-empty string"


def _opt_str(v):
    return None if v is None or (isinstance(v, str) and v.strip()) else "must be a non-empty string"


def _choice(*allowed):
    return lambda v: None if v in allowed else f"must be one of {', '.join(allowed)}"


def _int(lo):
    return lambda v: (None if isinstance(v, int) and not isinstance(v, bool) and v >= lo
                      else f"must be an integer >= {lo}")


def _opt_int(v):
    return None if v is None or (isinstance(v, int) and not isinstance(v, bool)) else "must be an integer"


def _opt_number(v):
    return (None if v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0)
            else "must be a number >= 0")


def _routes(v):
    from .retrieval.registry import ROUTE_KINDS

    if v is None:
        return None
    if not isinstance(v, list) or not v or len(set(v)) != len(v) or any(r not in ROUTE_KINDS for r in v):
        return f"must be a non-empty list of distinct routes from {', '.join(ROUTE_KINDS)}"
    return None


def _holdout(v):
    return (None if isinstance(v, bool) or (isinstance(v, list) and all(isinstance(x, str) for x in v))
            else "must be true/false or a list of function names")


#: The governed blocks of project.toml: section -> key -> (default, validator, env override or None, read).
#: ``read`` is True when the runtime reads the key (from the project or through its env override) and False when
#: the key is declared but has no runtime reader yet; the effective-configuration diagnostic says so.
#: Defaults follow what this checkout actually does: guided search, structural rerank, analysis, REA and the
#: optional retrieval routes stay off until measured (RECHECK-2026-10-16). An env override is the variable
#: that governs the behaviour today; when set it wins over the project value.
CONFIG_SCHEMA: dict[str, dict[str, tuple]] = {
    "matching": {
        "verification_policy": ("project_default", _str, None, False),
        "compile_cache": (True, _bool, None, False),
        "batch_compile_once": (True, _bool, None, False),
        "prepared_target_cache": (True, _bool, None, False),
        "score_only_permuter": (True, _bool, None, False),
    },
    "search": {
        "policy": ("single_v1", _choice("single_v1", "guided_v1"),
                   ("DECOMPNET_GUIDED_SEARCH", lambda e: "guided_v1" if e == "1" else "single_v1"), True),
        "structural_rerank": (False, _bool, ("DECOMPNET_STRUCTURAL_RERANK", lambda e: e == "1"), True),
        "seed": (None, _opt_int, None, False),
        "portfolio_size": (4, _int(1), None, False),
        "allow_exploratory_mutations": (True, _bool, None, False),
        "global_resource_budget": ("host_profile", _str, None, False),
    },
    "analysis": {
        "enabled": (False, _bool, None, False),
        "route": ("direct_ghidra", _choice("direct_ghidra", "rea"), None, False),
        "escalation_policy": ("structural_stall_v1", _str, None, False),
        "capability_manifest": (None, _opt_str, None, False),
        "persist_observations": (True, _bool, None, False),
        "persist_live_database": (False, _bool, None, False),
        "allow_target_byte_mutation": (False, _bool, None, False),
    },
    "rea": {
        "enabled": (False, _bool, ("DECOMPNET_REA_PROVIDER", lambda e: e == "1"), True),
        "artifact": (None, _opt_str, None, False),
        "schema_digest": (None, _opt_str, None, False),
        "read_only": (True, _bool, None, False),
    },
    "retrieval": {
        "exact_twins": (True, _bool, None, False),
        "lightweight_structure": (True, _bool, None, False),
        "function_id_retrieval": (False, _bool, None, True),
        "bsim_retrieval": (False, _bool, None, True),
        "external_queries": (False, _bool, None, True),
        "route_order": (None, _routes, None, True),
        "bsim_max_cold_cost": (None, _opt_number, None, True),
        "holdout": (False, _holdout, None, True),
    },
}

#: Values that are never allowed, whatever else is configured: the target is evidence, not something to edit,
#: and REA is only ever admitted read-only (plan 18.9, P9).
FORBIDDEN_VALUES = {
    ("analysis", "allow_target_byte_mutation"): (True, "analysis must never mutate target bytes"),
    ("rea", "read_only"): (False, "REA is only admitted read-only"),
}


def validate_config(cfg: dict) -> list[str]:
    """Validate the governed blocks (matching, search, analysis, rea, retrieval) of a project config.

    Raises :class:`ConfigError` for an unknown key inside a governed block (a typo must not silently enable
    or disable a gated route), a wrong type, or a forbidden value. Top-level keys stay free-form (ROM and
    fork configs carry many); the returned warnings name the ones this schema does not govern."""
    errors: list[str] = []
    for section, keys in CONFIG_SCHEMA.items():
        block = cfg.get(section)
        if block is None:
            continue
        if not isinstance(block, dict):
            errors.append(f"[{section}] must be a table")
            continue
        for key in sorted(set(block) - set(keys)):
            errors.append(f"unknown key {section}.{key} (known: {', '.join(keys)})")
        for key, value in block.items():
            if key in keys and (problem := keys[key][1](value)):
                errors.append(f"{section}.{key} {problem}")
            forbidden = FORBIDDEN_VALUES.get((section, key))
            if forbidden and value == forbidden[0] and isinstance(value, bool):
                errors.append(f"{section}.{key} = {str(value).lower()} is refused: {forbidden[1]}")
    rea = cfg.get("rea") if isinstance(cfg.get("rea"), dict) else {}
    if rea.get("enabled") is True and not (rea.get("artifact") and rea.get("schema_digest")):
        errors.append("rea.enabled needs a pinned rea.artifact and rea.schema_digest")
    if errors:
        raise ConfigError("invalid project configuration:\n  " + "\n  ".join(errors))
    warnings = []
    analysis = cfg.get("analysis") or {}
    if analysis.get("persist_live_database") is True:
        warnings.append("analysis.persist_live_database keeps a live analysis database on disk")
    return warnings


def effective_config(cfg: dict, env=None) -> dict:
    """The effective-configuration diagnostic: for every governed key, the value that applies and where it
    came from (default, project or env). Validates first, so an invalid config never gets a diagnostic."""
    import os

    env = os.environ if env is None else env
    warnings = validate_config(cfg)
    out: dict = {}
    for section, keys in CONFIG_SCHEMA.items():
        block = cfg.get(section) or {}
        out[section] = {}
        for key, (default, _, override, _read) in keys.items():
            value, source = default, "default"
            if key in block:
                value, source = block[key], "project"
            if override and env.get(override[0]) is not None:
                env_value = override[1](env[override[0]])
                if source == "project" and env_value != value:
                    warnings.append(f"{section}.{key}: project says {value!r}, {override[0]} makes it {env_value!r}")
                value, source = env_value, f"env:{override[0]}"
            out[section][key] = {"value": value, "source": source}
    return {"sections": out, "warnings": warnings}


def format_effective_config(diag: dict) -> str:
    lines = []
    for section, keys in diag["sections"].items():
        lines.append(f"[{section}]")
        for key, item in keys.items():
            note = "" if CONFIG_SCHEMA[section][key][3] else "  declared, not yet read at runtime"
            lines.append(f"  {key} = {json.dumps(item['value'])}  ({item['source']}){note}")
    lines += [f"warning: {w}" for w in diag["warnings"]]
    return "\n".join(lines)


def compiler_config(root: Path, cfg: dict) -> CompilerConfig:
    """Build the sandboxed compile command for a project.

    Either `cmd` (a full command with {src} {obj} {include} placeholders) or `toolchain` (a registry id,
    see toolchains.toml) plus optional `flags`. With a toolchain, `{tc}` expands to its install directory,
    which is mounted read-only into the sandbox together with any runtime it requires."""
    c = cfg["compiler"]
    ro_paths = [str((root / p).resolve()) for p in c.get("ro_paths", [])]
    runtime32 = None
    linux = False
    if "toolchain" in c:
        tc = toolchains.get(c["toolchain"])
        linux = tc.via_linux  # a Linux-only build: runs in the compiler container
        if linux and not (ok := toolchains.side_status())[0]:
            raise toolchains.ToolchainError(f"{tc.name} is a Linux program: {ok[1]}")
        dirs = toolchains.runtime_dirs(tc.id)
        tc_dir = str(dirs[0])
        ro_paths += [str(d) for d in dirs]
        if "runtime-i386" in tc.requires and not linux:  # the compiler container has its own 32-bit runtime
            runtime32 = str(toolchains.require("runtime-i386"))
        flags = c.get("flags", tc.flags)
        if c.get("cmd"):
            base = c["cmd"]
        elif tc.cmd:  # a full template ({flags} expands to the flags)
            base = [x for a in tc.cmd for x in (flags if a == "{flags}" else [a])]
        else:
            base = [*tc.cc, *flags, "-I{include}", "{src}", "-o", "{obj}"]
        cmd = [a.replace("{tc}", tc_dir) for a in base]
        for dep in tc.requires:  # {tc:<id>} is another toolchain this one needs
            where = toolchains.path(dep, linux)
            cmd = [a.replace(f"{{tc:{dep}}}", str(where)) for a in cmd]
    else:
        cmd = list(c["cmd"])
    return CompilerConfig(
        cmd=cmd,
        timeout=float(c.get("timeout", 15)),
        sandbox=c.get("sandbox", "auto"),
        image=c.get("image"),
        ro_paths=tuple(ro_paths),
        include_dirs=tuple(str((root / p).resolve()) for p in c.get("include_dirs", [])),
        runtime32=runtime32,
        linux=linux,
    )


#: The assembler variant used for the attempt-scoped fallback (see compiler.CompilerConfig.asm_cmd):
#: modern binutils + the PSY-Q `move` expansion. Tried only when a base attempt is a near miss.
ASM_VARIANT_PASS = "psyq-move-addu"
ASM_VARIANT_TOOLCHAIN = "binutils-mips"
#: The PSY-Q assembly preprocessor (mkst/maspsx, MIT): our pipeline ships it but, without this stage,
#: gcc's jump tables and %hi/%lo of local labels cannot be encoded by the bare assembler ("Illegal
#: operands lui/addiu"). Reference build recipe (celophi/lom-decomp, MIT): gcc -S | maspsx | as -G0.
MASPSX_TOOLCHAIN = "maspsx"


def maspsx_variant_config(root: Path, cfg: dict):
    """Compile through maspsx: gcc -S, the maspsx preprocessor, then the toolchain's own assembler.

    Returns None when maspsx or the project's compiler is unavailable."""
    from dataclasses import replace

    from . import toolchains

    try:
        base = compiler_config(root, cfg)
    except toolchains.ToolchainError:
        return None
    if base.asm_cmd is not None or not base.cmd:
        return None
    try:
        maspsx_dir = Path(toolchains.path(MASPSX_TOOLCHAIN))
    except toolchains.ToolchainError:
        return None
    script = maspsx_dir / "maspsx.py"
    if not script.exists():
        return None
    flags = list(cfg.get("compiler", {}).get("flags", []))
    g_flag = next((f for f in flags if f.startswith("-G")), None)
    pp = (sys.executable or "python3", str(script), *([g_flag] if g_flag else []))
    tc_dir = Path(base.cmd[0]).parent  # the expanded {tc} directory of the project's compiler
    asm = tc_dir / "as"
    if not asm.exists():
        return None
    endian = "-EB" if cfg.get("big_endian") else "-EL"
    # maspsx documents that GNU as wants -G0 for correct PSY-Q behaviour (its --dont-force-G0 opts out)
    return replace(base, asm_pp_cmd=tuple(pp), asm_cmd=(str(asm), endian, "-G0", "-o", "{obj}", "{src}"),
                   cache=False, ro_paths=tuple({*base.ro_paths, str(maspsx_dir)}))


def variant_compiler_config(root: Path, cfg: dict, *, asm_pass: str = ASM_VARIANT_PASS):
    """The same compile, assembled by the modern mips binutils with the PSY-Q passes applied.

    Returns None when the toolchain is not installed (the fallback then simply does not run)."""
    from dataclasses import replace

    from . import toolchains

    base = compiler_config(root, cfg)
    if base.asm_cmd is not None:
        return None
    try:
        asm_dir = Path(toolchains.path(ASM_VARIANT_TOOLCHAIN))
    except toolchains.ToolchainError:
        return None
    asm = asm_dir / "as"
    if not asm.exists():
        for candidate in ("mips-ps2-decompals-as", "mipsel-linux-gnu-as"):
            if (asm_dir / candidate).exists():
                asm = asm_dir / candidate
                break
        else:
            return None
    endian = "-EB" if cfg.get("big_endian") else "-EL"
    return replace(
        base,
        # -G0: the ROM uses absolute lui/%lo addressing for these globals; the modern assembler would
        # otherwise shrink them to %gp_rel (measured: 6790 vs 410 on slus-01363 func_800A51DC).
        asm_cmd=(str(asm), endian, "-G0", "-o", "{obj}", "{src}"),
        asm_pass=asm_pass,
        cache=False,
        ro_paths=tuple({*base.ro_paths, str(asm_dir)}),
    )


def target_objects(root: Path, cfg: dict) -> list[Path]:
    t = cfg["target"]
    if isinstance(t.get("build_from"), list):  # one object per source file, named after it
        return [root / "target" / f"{Path(src).stem}.o" for src in t["build_from"]]
    return [root / o for o in t["objects"]]


def build_target(root: Path, cfg: dict, log=print) -> None:
    """Build target objects from source the project is allowed to have (own code / open source),
    using the project's own sandboxed toolchain. Real ROM projects ship assembled targets instead."""
    t = cfg["target"]
    sources = t["build_from"] if isinstance(t["build_from"], list) else [t["build_from"]]
    outputs = target_objects(root, cfg)
    for src, out in zip(sources, outputs):
        cc = compiler_config(root, cfg)
        src_path = root / src
        cc.include_dirs = (*cc.include_dirs, str(src_path.parent))
        log(f"building {out.relative_to(root)} from {src} with {cfg['compiler'].get('toolchain', 'cmd')}")
        res = compile_source(cc, "", src_path.read_text(errors="replace", encoding="utf-8"), trusted=True)
        if not res.ok:
            raise ProjectRejected(f"target build failed for {src}:\n{res.log}")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(res.obj)


def import_project(store: Store, root: Path, log=print) -> int:
    """Create or refresh a project. Existing progress (matches, attempts) is kept;
    new functions are added; targets of unmatched functions are refreshed."""
    root = root.resolve()
    cfg = load_config(root)
    check_legal(cfg)
    for warning in validate_config(cfg):
        log(f"warning: {warning}")
    slug = cfg["slug"]
    target = cfg["target"]

    objects = target_objects(root, cfg)
    hash_only = target.get("verification", "full") == "hash"
    if "build_from" in target and not all(o.exists() for o in objects):
        build_target(root, cfg, log)
    elif "build" in target and not all(o.exists() for o in objects):
        log(f"building target objects: {' '.join(target['build'])}")
        subprocess.run(target["build"], cwd=root, check=True)

    funcs = []
    seen: dict[str, str] = {}
    arch, big_endian = None, False
    identity: dict[str, tuple] = {}  # name -> (artifact sha256, section, section offset), plan 18.2
    for obj in objects:
        data = obj.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        try:
            located = function_locations(data)
        except Exception:
            located = {}
        for name in list_functions(data):
            if name in cfg.get("exclude", []):
                continue
            if name in seen:
                raise ProjectRejected(f"function {name!r} exists in both {seen[name]} and {obj.name}; "
                                      f"add one of them to `exclude` in project.toml")
            seen[name] = obj.name
            unit = obj.stem
            section, offset = located.get(name, (None, None))
            identity[name] = (digest, section, offset)
            fn = extract_function(data, name)
            arch, big_endian = fn.arch, fn.big_endian
            insns = scoring.disassemble(fn)
            seed_file = root / "seeds" / f"{name}.c"
            funcs.append([name, fn, len(insns), scoring.asm_text(fn),
                          seed_file.read_text(encoding="utf-8") if seed_file.exists() else None, unit])

    header = (root / cfg["context"]["file"]).read_text(encoding="utf-8")
    # Optional per-unit headers: [context] units = "units/{unit}.h" (one per source file / object)
    unit_headers = {}
    if cfg["context"].get("units"):
        for obj in objects:
            path = root / cfg["context"]["units"].format(unit=obj.stem)
            if path.exists():
                unit_headers[obj.stem] = path.read_text(encoding="utf-8")
    header_of = lambda f: unit_headers.get(f[5], header)
    # Functions without a hand-written seed get a machine-generated starting point (m2c), when available.
    tc_id = cfg["compiler"].get("toolchain")
    missing = [f for f in funcs if f[4] is None]
    if missing and not hash_only and tc_id in seeds.M2C_TARGETS and seeds.available():
        with ThreadPoolExecutor(max_workers=4) as pool:
            drafts = list(pool.map(lambda f: seeds.generate(f[1], tc_id, header_of(f)), missing))
        for f, draft in zip(missing, drafts):
            f[4] = f"/* machine-generated by m2c: a starting point, not a match */\n{draft}" if draft else None
        log(f"m2c drafted {sum(d is not None for d in drafts)}/{len(missing)} functions")
    now = time.time()
    with store.tx() as db:
        row = db.execute("SELECT id FROM projects WHERE slug=?", (slug,)).fetchone()
        if row is None:
            pid = db.execute(
                "INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                (slug, cfg.get("name", slug), cfg.get("description", ""), arch, big_endian, str(root), json.dumps(cfg)),
            ).lastrowid
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (pid, 1, header, None, "initial import", now, ""))
        else:
            pid = row["id"]
            db.execute(
                "UPDATE projects SET name=?,description=?,arch=?,big_endian=?,root=?,config=? WHERE id=?",
                (cfg.get("name", slug), cfg.get("description", ""), arch, big_endian, str(root), json.dumps(cfg), pid),
            )
        have = {r["unit"] for r in db.execute("SELECT DISTINCT unit FROM context_versions WHERE project_id=?", (pid,))}
        for unit, text in unit_headers.items():
            if unit not in have:
                version = db.execute("SELECT MAX(version) AS v FROM context_versions WHERE project_id=?", (pid,)).fetchone()["v"] + 1
                db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                           "VALUES(?,?,?,?,?,?,?)", (pid, version, text, None, f"initial import ({unit})", now, unit))
        for name, fn, n_insns, asm, seed, unit in funcs:
            if hash_only:  # keep only a one-way fingerprint and the size, never the code or its disassembly
                target_bytes, relocs, asm, seed = None, "[]", "", None
            else:
                target_bytes, relocs = fn.code, json.dumps([r.to_json() for r in fn.relocs])
            db.execute(
                """INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,fingerprint,unit)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(project_id,name) DO UPDATE SET size=excluded.size, n_insns=excluded.n_insns,
                   target=excluded.target, relocs=excluded.relocs, asm=excluded.asm, seed=excluded.seed,
                   fingerprint=excluded.fingerprint, unit=excluded.unit
                   WHERE functions.status != 'matched'""",
                (pid, name, len(fn.code), n_insns, target_bytes, relocs, asm, seed, fn.fingerprint(), unit),
            )
            row = db.execute("SELECT f.id, f.status, i.recorded_by FROM functions f LEFT JOIN function_identity i "
                             "ON i.function_id = f.id WHERE f.project_id=? AND f.name=?", (pid, name)).fetchone()
            # a matched function keeps its old target, so it keeps the identity it was matched under
            if row["status"] != "matched" or row["recorded_by"] != "import":
                digest, section, offset = identity[name]
                Store.record_identity(db, row["id"], artifact_sha256=digest, module_id="main", unit_id=unit,
                                      address_space=f"object:{unit}",
                                      entry_kind="section_offset" if section else "unknown",
                                      entry=hex_entry(offset), section=section)
    log(f"project {slug!r}: {len(funcs)} functions ({arch})")
    return pid
