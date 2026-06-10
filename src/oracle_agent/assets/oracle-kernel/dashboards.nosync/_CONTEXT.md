# dashboards.nosync

Rendered dashboards, reports, and generated visual outputs.

## Discipline

- Dashboards are **deliverables or views, not durable atomic memory.** Decompose any durable claim a dashboard surfaces into `Memory.nosync/`; the dashboard is a rendering, not the record.
- A dashboard's claims should trace back to evidence through the answer protocol — a number on a chart with no authoritative source behind it does not belong here.
- Regenerate rather than hand-edit: a dashboard is a function of its underlying data and should be reproducible from it.
- Cadenced, recurring deliverables belong in `Workproduct.nosync/_STANDING/` and ship through `artifact_io.emit` under the policy gate; this folder is for the rendered visual layer.

## The admin dashboard

- `./oracle dashboard` renders the admin systems dashboard in-session; `./oracle dashboard publish` writes `admin-dashboard.html` here (self-contained, no external assets — regenerate, never hand-edit).
- `layout.yml` (optional, block-style YAML; `order:` / `hidden:` lists of panel keys from `./oracle dashboard panels`) is the hand-editable INPUT that evolves the dashboard under self-improvement: propose layout/panel changes through the improvement lifecycle, apply by editing this file (advisory: agent-obeyed, not code-enforced). Missing or invalid layout.yml falls back to defaults.
