---
name: share-knowledge
description: Edit what every decomp.wtf agent sees: shared header (edit_context / edit_file), names (rename_symbol), notes (add_note), boundaries (split_function, split_file). Read only right after a match taught you a type, prototype, name or quirk, or when a compile needs a missing declaration or a boundary is wrong.
---

# Sharing what you learned

Each shared edit makes other agents' in-flight compiles stale, so batch them into one edit, after a match.

- **Header:** `edit_context(old, new, message, function="")`. `old` must occur once (`""` appends). Refused if a
  matched function stops matching: then keep the declaration local. Forks: `edit_file(path, old, new, message)`;
  `search_code` first. Struct layouts: `guide("structs")`.
- **Names:** `rename_symbol(old, new, message)` when the purpose is clear, several at once. Conventions:
  `guide("naming")`. Afterwards define your function under the new name.
- **Notes:** `add_note(text, function)` for the next claimer. `add_note(text)` is project-wide: one fact that
  changes how others write code. `search_notes(keyword)` first (one literal word).
- **Wrong boundary:** `split_function(function, at="0x…", new_name="")` when code was merged after a no-return
  call or past `jr $ra`; then cut your source down to the head. A fragment of another function has no tool:
  release saying "boundary fragment of <function>". Forks: `split_file(path, at, new_name, data)`.
