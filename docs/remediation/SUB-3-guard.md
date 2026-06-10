# SUB-3 — No-Bypass Guard Extension (Phase 3)

Single task G1, single agent, runs after Phases 1–2 (it adds markers to files
they touched).

**Files:** `src/oracle_agent/assets/oracle-kernel/tests/test_no_bypass_guard.py`,
plus marker comments in kernel `_tools/*.py` files at legitimate write sites.

**Finding (verified):** the AST guard flags only
`shutil.move/copy/copy2/copyfile` and `open(<non-literal>, write-mode)`. It
misses the kernel's most common write method — `Path.write_text` /
`write_bytes` — plus `os.replace`, `os.rename`, `shutil.copytree`. The
"single chokepoint, structurally enforced" guarantee is therefore not
structural: new code (e.g. a future gateway adapter) could write to a
model-influenced path and CI stays green. Known legit sites today (all feed
`safe_paths.contain()` outputs): `truth_map.py` (rewritten in Phase 1),
`session_memory.py:145,709-745`, `derived_memory.py:413-420`,
`source_record.py:411`, `ledger.py` internals, migrations.

**Required behavior:**
1. Extend the guard's AST visitor to flag, on non-literal targets:
   `<expr>.write_text(...)`, `<expr>.write_bytes(...)`, `os.replace`,
   `os.rename`, `shutil.copytree`.
2. Follow the guard's EXISTING exemption mechanism for legitimate sites
   (inspect how current exemptions are expressed — marker comment, allowlist
   in the test, or wrapper function — and use the same idiom; do not invent a
   second mechanism). Each exemption must be at a site whose path provably
   came through `safe_paths.contain()`/the root-confined helpers; if you find
   a write site that does NOT, do not exempt it — report it as `BLOCKER:` in
   your final output.
3. The guard test must fail on a synthetic fixture exercising each newly
   covered pattern (test-the-test), and pass on the real tree.

**Acceptance:** guard covers the five new write forms; all kernel files pass
with explicit, justified exemptions; `make check` green.
