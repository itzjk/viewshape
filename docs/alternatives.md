# Exact-case comparison

Checked on 2026-10-29 against the committed, synthetic demo snapshots. This is
a bounded comparison of the named tools, not evidence that no alternative
exists or that production incidents are common.

## sqlite-utils 4.2.1

Commands run:

```sh
sqlite-utils views examples/snapshots/before.sqlite --schema
sqlite-utils views examples/snapshots/after.sqlite --schema
sqlite-utils views examples/snapshots/before.sqlite --columns --schema
sqlite-utils views examples/snapshots/after.sqlite --columns --schema
```

The two `--schema` results are identical. With `--columns`, `public_items`
changes from `["id", "title"]` to `["id", "title", "state"]`; `explicit_items`
keeps `["id", "title"]`. This is a useful metadata primitive. Its documented
[view listing](https://sqlite-utils.datasette.io/en/stable/cli.html#listing-views)
does not produce a two-snapshot report with column types, classified changes,
per-side errors, coverage, or CI comparison exit codes. Shell diffing two
listings remains a reasonable alternative for a quick manual check.

## DBDiff 3.0.0-rc.31

Installed the published `@dbdiff/cli` package and ran its bundled macOS binary
on private copies of these same snapshots. Source is the desired schema,
target is the old schema:

```sh
dbdiff --driver=sqlite --type=schema --output=demo.sql \
  server1.after.sqlite:server1.before.sqlite
```

The command completed and generated this migration statement:

```sql
ALTER TABLE "items" ADD COLUMN "state" text;
```

It did not report the resolved output change of `public_items`, the unchanged
output of `explicit_items`, or either view's ordered metadata. That is the
specific question viewshape answers. DBDiff remains the broader migration tool.

Source inspected at commit
[`d2e77a9`](https://github.com/DBDiff/DBDiff/tree/d2e77a976c7115e37f04a909488e81bb688188ff):
[`SQLiteAdapter::getViews`](https://github.com/DBDiff/DBDiff/blob/d2e77a976c7115e37f04a909488e81bb688188ff/src/DB/Adapters/SQLiteAdapter.php)
returns names and stored SQL;
[`DBSchema::diffViews`](https://github.com/DBDiff/DBDiff/blob/d2e77a976c7115e37f04a909488e81bb688188ff/src/DB/Schema/DBSchema.php)
compares those definition strings for common names.

## Other boundaries

[sqldiff](https://www.sqlite.org/sqldiff.html#limitations), documentation updated
2023-01-06 and read 2026-10-29, explicitly excludes view differences in normal
operation. Comparing `sqlite_schema` can detect changed definitions, but these
two definitions stay identical.

[sqlite-schema-probe](https://github.com/KanadeK/sqlite-schema-probe) is the
brief's reference for finding broken view references. viewshape's primary
purpose is comparing resolved outputs across versions; resolution errors are
coverage gaps rather than its headline feature. That repository could not be
retrieved during this implementation check, so its current capabilities are
not verified here.

The brief's Graphify attention signal concerns code and schema comprehension;
it is not measured SQLite demand. The reason to build this small tool is the
reproduced metadata gap, not a claim about market size.
