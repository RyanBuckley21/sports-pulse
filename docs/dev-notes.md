# Dev notes & process learnings

Running log of engineering process lessons for Sports Pulse. Add to the top.

---

## 2026-07-20 — False "verified cleanly" from a self-killing teardown

**What happened.** Two Phase 2 verification tasks (Playwright renders of the
Insights pages) showed up in the task panel as **Failed** with `Exit code 144`,
even though the render checks inside them had actually printed passing results.
I summarized the work as "verified cleanly" without reconciling that non-zero
exit or reading the full output.

**Root causes (two, compounding).**
1. **Teardown killed its own shell.** The cleanup line was
   `pkill -f "http.server 82NN"`. `pkill -f` matches a process's *entire command
   line*, and the shell executing the script contained that literal string — so
   `pkill` SIGTERM'd its own parent shell, yielding `128 + 16 = 144` *after* the
   verification had already succeeded and printed. The failure was in teardown,
   not in the product.
2. **Summarized instead of verified.** I reported pass/fail in prose and waved
   off a console-error list as "external fonts/favicon" without proving it,
   while letting a non-zero exit code stand unexamined.

**Resolution.** Re-ran verification killing the server **by PID** (`SRV=$!` …
`kill "$SRV"`), captured per-page DOM counts, `pageerror`s, failed requests
(with URLs), and an explicit `node exit code`. Result: exit 0, no our-code
errors on any page; the only console noise was the sandbox-blocked Google Fonts
CDN and a `/favicon.ico` 404 (later fixed by adding a favicon link).

**Lessons (apply going forward).**
- **Verification must report real evidence, not a verdict.** Paste actual output
  and the actual **exit code**; never claim "verified"/"passes" from a
  summarized impression. A non-zero exit is unresolved until explained.
- **Distinguish our-code errors from environmental noise explicitly** (blocked
  CDNs, favicon 404s) — and prove the classification (e.g. list failed request
  URLs), don't assert it.
- **Tear down background servers by PID, not by pattern.** Capture the PID at
  launch (`server & SRV=$!`) and `kill "$SRV"`. Never `pkill -f <pattern>` where
  the pattern can appear in the running shell's own command line.
