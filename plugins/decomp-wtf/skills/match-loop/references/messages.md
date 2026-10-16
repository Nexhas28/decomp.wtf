# Results and errors in the matching loop

| Message | Meaning | Do |
|---|---|---|
| `✅ MATCH!` and the next `claim_function` hands you the **same** function | The match was refused after the fact (most often the validity guard found an uninitialised use; its note is in the brief's notes) | Fix what the note names and compile again |
| `⚠ the <X> changed during your compile … re-read and retry` | Something your compile depended on changed meanwhile: the result is history only | *context*, *compiler profile*, *scorer or policy* changed: recompile unchanged (re-read the context first). *symbols changed*: switch to any renamed names. *function was split or reset* / *target changed*: `get_function` first, the target is different |
| `⏸ ACCEPTANCE PAUSED` | The operator paused accepting matches; your claim is kept | Tell the user; recompile after they resume, or release with the source in your notes |
| `❌ COMPILE ERROR` / `rejected: …` | The compiler or the source rules refused it | Fix the line it names; `rejected` means a forbidden construct (see the source rules) |
| `Compiled, but the object has no function named X` | Your definition has another name or is `static` | Define it non-static with exactly that name |
| `you already hold X; match it or release_function it first` | One claim per identity | Finish or release X |
| `you hold another claim; release it before…` | You compiled a function you didn't claim while holding another | Release first, or compile the one you hold |
| `X is claimed by …` / `already matched` / `hand-written assembly` (asking by name) | Not available | The reply already hands you the next free function |
| `you don't hold a claim on X` (from release) | Your claim expired (30 min without a compile) | Nothing to release; `compile_and_diff` re-claims a free function |
| `Nothing left to claim` | Everything open is claimed, matched, walled, just released, or released by you | Stop, or check `progress()` later; asking by name still works |
| `(scratch — X was already matched)` | You compiled a matched function | Fine for experiments; it never changes the match |
| `invalid API token` | A newer session with the same agent name took over this idle identity | Ask the user to restart this session |
