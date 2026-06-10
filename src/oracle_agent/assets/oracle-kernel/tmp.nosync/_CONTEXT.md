# Runtime Scratch

Ephemeral scratch space for temporary files created during local oracle
operations. Nothing in this directory is authoritative memory, durable
workproduct, or backup-critical state.

Agents may write short-lived intermediate files here when a tool explicitly
needs a scratch location. Durable records must be written to `Memory.nosync/`,
`Meta.nosync/`, `Workproduct.nosync/`, or `_data.nosync/` through the
appropriate tool.
