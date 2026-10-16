# Phase 12 rollout record

Canary-first staging, executed 16 Oct 2026:

1. Fixture lanes: full test suite (388 passed; environment lanes recorded).
2. Private canary: fork wheel (dist/decompnet-0.10.6-py3-none-any.whl) installed in
   /Users/shanemcgovern/Decomp/canary-venv; ran with DECOMP_HOME=canary-home on port 19388 against a
   WAL-safe copy of the live DB (canary-home/.decomp/decomp.db).
   Evidence the canary ran the fork:
   - the copied DB gained the fork's tables on open (attempt_snapshots, candidates,
     search_budgets, verification_receipts — absent from the stock live DB);
   - an end-to-end MCP smoke claim + real gcc-2.8.1-psx compile on the copy scored func_800A1D6C
     (78210) and recorded an attempt WITH an attempt_snapshots row (fork-only behavior);
   - 30-tool MCP surface answered project_info correctly on the copied project.
3. Representative project: the canary ran on the real slus-01363 copy (1208 matched at copy time).
4. Production cutover: rollout.sh (WAL-safe pre-cutover backup -> stop -> install fork wheel into
   the live uv tool venv -> restart -> health check). Rollback: reinstall the stock wheel from
   https://decomp.wtf/dl/decompnet-0.10.6-py3-none-any.whl and restore the pre-cutover DB backup
   (benchmarks/RUNBOOKS.md 24.0: restore via sqlite backup API).

CUTOVER EXECUTED 16 Oct 2026:
- pre-cutover backup: /Users/shanemcgovern/Decomp/.decomp/backups/pre-cutover-20261016-001806.db (sqlite backup API)
- live app stopped, fork wheel installed into ~/.local/share/uv/tools/decompnet via `uv pip install --reinstall --no-deps`
- restart raced the swarm bridges (they self-heal via ensure_app): two instances briefly existed
  (6464/6465); both killed; bridges restarted exactly one fork instance
- verified: single listener pid on 6464, /health ok, live DB gained the fork's tables
  (attempt_snapshots/candidates/search_budgets/verification_receipts), and the swarm (14 workers,
  1210/1661 matched) continues matching through the fork's acceptance path
- rollback: reinstall the stock wheel (https://decomp.wtf/dl/decompnet-0.10.6-py3-none-any.whl) and
  restore the pre-cutover backup per benchmarks/RUNBOOKS.md 24.0
