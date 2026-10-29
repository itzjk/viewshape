# Reproduce the unchanged-definition case

The committed snapshots contain no user rows. Each has an `items` table and
two views: `public_items AS SELECT * FROM items` and
`explicit_items AS SELECT id, title FROM items`. Only the second snapshot's
table has `state TEXT`.

From an installed clone, regenerate the fixtures in a new directory:

```sh
python examples/make_snapshots.py demo-snapshots
viewshape demo-snapshots/before.sqlite demo-snapshots/after.sqlite --json
```

The generator refuses existing files. It prints SQLite's version, the stored
view definitions, ordered column metadata, and each `integrity_check` result.
Both checks return `ok`. Those checks validate structure, not view output
compatibility. The comparison exits 1 because `public_items` changes from
`id, title` to `id, title, state`; `explicit_items` remains unchanged.

[metadata.json](metadata.json) records the generator's SQLite 3.51.0 run.
[report.json](report.json) records a viewshape run on the same files. Engine
versions can differ between the generation and inspection environments;
each artifact records its own engine.
