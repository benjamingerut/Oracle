# People

Org, hiring, roles, policy, culture, staffing, and people-related artifacts.

People data is sensitivity-heavy: classify drops at log time (often `confidential` or `restricted`) and minimize sensitive personal detail when decomposing into memory.

Use `received/` for inbound artifacts and `created/` for oracle-produced artifacts. Both subfolders are created at runtime when first used; all writes are contained to this lane via `safe_paths.assert_lane`.
