# Report format 1

`viewshape before.sqlite after.sqlite --json` writes one JSON object to stdout.
Argument syntax errors go to stderr and exit 3. Array order is deterministic:
views by exact name, columns by zero-based SQLite `cid`, changes by the old
column order followed by newly added columns in their new order. File hashes
identify the bytes inspected. Reports contain schema SQL, which may include
literal constants or sensitive object names; choose snapshots accordingly.

## Top-level fields

| Field | Meaning |
| --- | --- |
| `format_version` | Integer 1, the JSON contract |
| `tool_version`, `sqlite_version` | Inspector and local engine versions |
| `scope`, `limits` | What was compared and all configured budgets |
| `status`, `exit_code` | Observed result and process outcome |
| `differences_observed` | Known differences, even if inspection is incomplete |
| `empty` | Both inventories completed with zero views |
| `snapshots` | Per-side SHA-256, file size, and coverage |
| `views` | Per-view observations, including unchanged views |
| `errors` | Side-level or comparison-level errors |

Codes: invalid input is 3 and stops inspection, incomplete is 2, observed
differences are 1, and no observed differences or an empty comparison is 0.
An unsupported SQLite engine is incomplete (2). Exit 0 never certifies a
migration or compares SELECT results.

Each snapshot's coverage includes total schema objects, total views, inspected,
resolved, failed, omitted, and `inventory_complete`. Unknown counts are null.
Every omitted or failed view prevents a complete comparison. A worker timeout
retains observations already flushed to the parent process. An incomplete
inventory cannot establish that an unseen view is absent.

## View observations

Each view has `name`, `before`, `after`, `definition_changed`, `output_changed`,
`status`, and `changes`. A side is null when absent or not inspected; check
coverage before interpreting it as absence. A present side includes the stored
`sql`, ordered `columns`, and resolution `status`. A column has `position`,
`name`, and `declared_type`. Empty type strings stay empty. No runtime types,
nullability, or semantic equivalence are inferred.

Resolved comparison statuses are `unchanged`, `view-added`, `view-removed`,
`definition-unchanged/output-changed`, `definition-changed/output-unchanged`, and
`definition-changed/output-changed`. Incomplete observations are `unverified`.
Unknown change flags are null. A definition difference can still be observed
when output is unverified. Added or removed views can be identified even when
their output fails, but the comparison remains incomplete.

Changes are `column-added`, `column-removed`, `position-changed`, or
`declared-type-changed`. Exact column names match observations across sides;
names are never interpreted as renames. Position shifts caused by insertion
are still position changes.

Resolution failures keep `columns: null`, the original engine message in
`error.message`, and `sqlite_errorname` when Python exposes it:

- `unresolvable`: missing tables/columns, circular views, or an otherwise
  unclassified engine resolution error.
- `environment-required`: explicit missing-function/collation or unsafe-use
  evidence, or an authorization restriction.
- `unsupported`: a missing virtual module or a virtual-table dependency denied
  during preparation.
- `limit-exceeded`: budget, size, or engine complexity limits.

These categories describe observed diagnostics, not proven causes of every
failure. Unavailable syntax may yield `unresolvable` when SQLite provides no
more specific evidence. Metadata success alone does not prove an application's
runtime environment exists. SQL above 262144 bytes is omitted with `sql: null`
and `sql_omitted`; its definition is not compared as equal.

## Isolation and budgets

Only closed, coherent SQLite 3 files are accepted. Nonempty `-wal` and
`-journal` sidecars are checked before and after copying. Zero-byte sidecars
are permitted. Originals are never opened through SQLite. Identity, length,
mtime, ctime, and a second hash pass must agree. Copies use read-only SQLite
connections without `immutable=1`. Files and temporary worker logs are removed
when inspection ends. Stability checks cannot establish source coherence or
exclude all adversarial filesystem races. Consolidate and close databases with
your own tools before supplying them.

Defaults: 256 MiB per file, 1000 schema objects per side, 10 seconds for each
copy/verification stage and each inspection worker, and 2 MiB of rendered
output. SQLite allows at most 2000 columns and inspected view SQL of 256 KiB.
Schema loading is bounded by the configured file size. Extension loading is
disabled, attaching databases is denied, temporary SQL storage stays in memory,
and SQLite's cache budget is 2 MiB per worker. No project code or extensions are
loaded. Workers use Python's isolated interpreter mode, excluding the working
directory, `PYTHONPATH`, and user startup configuration from worker imports.
Built-in virtual table dependencies are also left unverified. Integer CLI
budgets must be between 1 and 9223372036854775807; SQLite's own value and SQL
limits are additionally clamped to the engine's compiled limits.

SQLite's progress callback and a parent-process watchdog limit inspection.
As with ordinary file tools, a kernel-blocked filesystem read is outside the
Python copy deadline. Use local regular files. Limits are resource budgets,
not a hardened sandbox against arbitrary native-engine vulnerabilities.

Worker metadata uses a quarter of the report budget per side. If that budget
is exceeded, remaining views are explicitly omitted and the comparison is
incomplete. If rendering exceeds the output budget, the report keeps summary
coverage and `differences_observed`, clears details, and adds
`report_views_omitted` and a limit error. Nothing omitted counts as matching.
Human output escapes control characters; JSON escapes Unicode for portable
terminal and log handling.
