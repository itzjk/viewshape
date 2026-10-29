# viewshape

Catch SQLite view columns that changed while their CREATE VIEW stayed the same.

[![CI](https://github.com/itzjk/viewshape/actions/workflows/ci.yml/badge.svg)](https://github.com/itzjk/viewshape/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

![A SELECT star view gains a column while an explicit projection stays unchanged](docs/demo.gif)

## Quick start

Requires Python 3.11+, SQLite 3.37.0+ in Python, and [pipx](https://pipx.pypa.io/).

```sh
pipx install git+https://github.com/itzjk/viewshape.git
```

Point it at two existing, closed database snapshots:

```sh
viewshape before.sqlite after.sqlite
```

## Features

- Detect output changes hidden behind identical view definitions.
- Compare ordered column names and SQLite's declared types, including empty types.
- See added or removed views, columns, position changes, and type changes.
- Keep useful results when another view cannot resolve, with errors attached to each side.
- Feed a versioned JSON report and distinct exit codes into CI.
- Inspect private copies while leaving source files and their directories untouched.

## Why

A migration can add a table column and change every `SELECT *` view above it without touching their stored SQL. SQLite exposes these resolved columns through [table_xinfo](https://www.sqlite.org/pragma.html#pragma_table_xinfo), while [sqldiff documents that it does not display view differences](https://www.sqlite.org/sqldiff.html#limitations). The [reproducible comparison](docs/alternatives.md) checks general tools against this exact case; an added output column is an observation for developers to assess, rather than an automatic compatibility verdict.

## How it works

viewshape validates each file, rejects nonempty WAL or rollback-journal sidecars, and copies accepted snapshots into a private temporary directory. It checks file identity, size, timestamps, and a second content hash for stability. The same local SQLite engine opens both copies read-only, enumerates `main` views, and queries parameterized `pragma_table_xinfo`. It also prepares an `EXPLAIN SELECT` for each quoted view name to detect unavailable functions, collations, and virtual-table dependencies without running the view. Nested views reflect the metadata SQLite resolves; the report makes no claim about which dependency caused a change.

## Usage

| Option | Purpose and default |
| --- | --- |
| `--json` | Emit the report with `format_version: 1` |
| `--max-bytes N` | Maximum bytes per snapshot, 268435456 |
| `--max-objects N` | Maximum schema objects per snapshot, 1000 |
| `--timeout S` | Seconds per copying or inspection stage, 10; maximum 300 |
| `--max-output-bytes N` | Report budget, 2097152; minimum 4096 |
| `--version` | Print the tool version |

Exit codes: **0** means no differences observed, **1** means differences, **2** means incomplete comparison, and **3** means invalid input or arguments. Invalid input takes precedence; otherwise incomplete wins over differences. JSON retains `differences_observed` when both changes and resolution errors occur. Zero views produces an explicit empty comparison with exit 0, which does not certify a migration.

The [report reference](docs/report.md) describes statuses, coverage, and safety limits. Both definitions and ordered columns appear in human and JSON output. A changed definition with unchanged columns is reported as `definition-changed/output-unchanged`, without asserting equal results.

## FAQ

**Can I try it without my own databases?** From a clone with the package installed, run:

```sh
viewshape examples/snapshots/before.sqlite examples/snapshots/after.sqlite
```

Only `public_items` changes output. Both fixtures pass `integrity_check`. The [fixture guide](examples/README.md) includes the generator and exact reproduction command.

**Does it modify my databases?** Originals are read as files. SQLite only opens private copies; extension loading and attached databases are disabled, with `trusted_schema=OFF` and `query_only=ON`.

**Does it infer renames or runtime types?** No. Renames appear as removal and addition. Types are exactly the declared strings SQLite returns, without inferred nullability or application contracts.

## Limitations

- Provide coherent, closed snapshots with consolidated WAL/journals. A stable copy cannot prove a concurrent source was coherent.
- Only persisted views in `main` are compared. Application functions, collations, virtual tables, and unsupported syntax can leave views unverified.
- Output can depend on SQLite's version. Record the reported engine version when reviewing results.
- No user rows, SELECT semantics, performance, migrations, repairs, or other database engines are compared.

## Contributing

Install from a clone with `python -m pip install .`, then run `python -m unittest discover -s tests -v`; add synthetic SQLite integration cases for changes.

## License

[MIT](LICENSE).
