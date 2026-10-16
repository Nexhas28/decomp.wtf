# Web UI code review (2026-10-16)

Scope: src/decompnet/app/static/ (app.js, index.html, app.css) and src/decompnet/app/server.py. Existing tests: 20 passed, 5 skipped. Every frontend API call matches a server route. Items 1-11, 16, 17 and 18 were re-checked against the code; (reproduced) means actually run. The rest are from the line-by-line review and are likely but not individually confirmed.

## Status: items 1-17 fixed and tested on 2026-10-16; items 18-32 not yet fixed

## High — users lose work or see the wrong thing
1. Try it wipes the user's edits when the attempt isn't a new best — app.js:2181-2183 sets editDirty=false then refreshFn → setEditor(d.source), and engine.py:2515 returns matched/best source, not the one just tried.
2. Opening function B while A is still loading leaves A on screen (and Try it then compiles A's code against B) — app.js:2087-2093 (single fnBusy lock drops B's fetch, then A's reply is discarded).
3. Switching projects before one finishes loading leaks a permanent 1 s poll + 8 s stats timer and can show the wrong project's header/tabs — app.js:520-554 (no `S.slug === slug` check after awaits).
4. Esc while typing in the code editor closes the drawer and discards edits — app.js:2401 (Escape handled before the "typing in an input" guard).
5. Deleting one project stops every agent on every project, and doesn't stop that project's autopilot/permuter — server.py:716-723 (`manager.stop()` with no id).

## Medium
6. Code containing an apostrophe shows literal "&#39;" in the highlighted editor and the overlay drifts from the caret (reproduced) — app.js:2043-2049, number regex runs on escaped text.
7. Team room: a host's `matched` value is inserted unescaped into the guest's page — app.js:1142. CSP (script-src 'self') blocks scripts, so it's HTML injection (fake buttons/text), not code execution.
8. A finished Try it lands on whatever function is now open (message, claim, dirty flag) — app.js:2165-2187.
9. Fork dialog: a slow policy reply from a cancelled dialog can enable Fork on another project without the "no AI code" acknowledgement — app.js:1389-1395.
10. Bad numbers in settings/launcher fields (context size, sessions, budget) give "request failed (500)" instead of the friendly message, and leave memory and disk out of sync — server.py:1059-1087.
11. Settings file is saved non-atomically from several threads; a crash mid-write makes the next start forget all endpoints and API keys — server.py:138-143. Also briefly world-readable on first create.
12. One failed job poll shows "Import failed" while the import keeps running and succeeds — app.js:56-65 (no retry).
13. Team tab rebuilds itself every 4-5 s, so clicks on Let in / Remove / Copy can be lost — app.js:1118, 1130-1134.
14. Retrying fork setup with the same file does nothing — app.js:1467-1474 (input value never cleared).
15. Cancelled big uploads leave partial files (GBs) behind; uploads also block the server while writing — server.py:583-590, 847-854, 1324-1331.
16. One project with a corrupt saved config stops the whole app from starting — server.py:1198-1204 (json.loads outside try).
17. Room server (reachable by anyone with the tunnel/LAN address): body size limit only checks Content-Length; a chunked request is read fully into memory and can exhaust it — collab.py:305-308.

## Low
18. N64 ROMs whose size isn't a multiple of 4 are rejected as "not a game file" (reproduced) — app.js:241-245.
19. If a project's map fails to load, the page stays blank and never updates, with no message — app.js:550, 125.
20. First poll on a new project can be skipped → flood of old "matched by…" toasts — app.js:551, 620-623.
21. /api/live returns only the newest 60 events/80 attempts; older ones (incl. "rescan") are silently skipped — engine.py:2550-2556.
22. Latest/Best toggle keeps the old highlight when opening another function; slow diff can overwrite the newer one — app.js:2058, 2285-2295.
23. j/k skips a row when the open function isn't in the filtered list — app.js:2301-2302.
24. "Leave room" doesn't refresh the view; "Stop sharing" has no error message — app.js:1151-1154, 1217-1220.
25. A failed catalog load is cached as empty until reload — app.js:1309, 1321. (Unchecked `javascript:` repo links at app.js:1381 are already blocked by the page's security policy.)
26. "Only ★ Next" launcher choice carries over to other projects — app.js:1728.
27. Home page shows nothing (no error) if /api/state fails; one broken project can 500 /api/state — app.js:185-191, server.py:455-459.
28. Path-import dialog leaves the page scroll-locked if the import fails — app.js:510-514.
29. Team timer keeps polling after switching to a project in fork setup — app.js:520-548.
30. Job logs and finished jobs are never cleaned up (memory grows) — server.py:88-124.
31. Windows only: /assets/ path check allows drive paths like C:/… — server.py:253-257.
32. Per-function compiler line never shows for ROM projects (server never sends it) — app.js:2098 vs engine.py:2493-2524.

Checked and fine: login/session protection on every API route, blocking of other websites, path handling for export/open/delete, escaping everywhere else, localStorage handling.
