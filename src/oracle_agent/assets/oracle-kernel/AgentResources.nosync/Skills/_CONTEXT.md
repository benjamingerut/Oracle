# Skills

Managed oracle-local skills live here. A skill is a reusable procedural memory
package for agents working inside this oracle. Skills are about how to do work,
not company facts; company facts belong in `Memory.nosync/`.

Each direct child directory under `Skills/` is one package:

```text
Skills/
  pricing-review/
    SKILL.md
```

`SKILL.md` uses block-style YAML frontmatter and a non-empty Markdown body.
Managed lifecycle changes go through `_tools/skills.py` / `oracle skills`, which
appends metadata-only rows to `Meta.nosync/ledgers/skill_event.jsonl`.

Deletion is not part of the managed lifecycle. Obsolete skills are archived under
`Skills/.archive/` so history remains recoverable.
