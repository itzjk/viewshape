import argparse
import json
import sqlite3
from pathlib import Path


def generate(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result = {}
    for side, extra in (("before", ""), ("after", ", state TEXT")):
        path = directory / (side + ".sqlite")
        if path.exists():
            raise FileExistsError(path)
        with sqlite3.connect(path) as connection:
            connection.executescript(
                "CREATE TABLE items(id INTEGER, title TEXT" + extra + ");"
                "CREATE VIEW public_items AS SELECT * FROM items;"
                "CREATE VIEW explicit_items AS SELECT id, title FROM items;"
            )
            result[side] = {
                "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
                "views": {
                    name: {"sql": sql, "columns": connection.execute(
                        "SELECT cid, name, type FROM pragma_table_xinfo(?, 'main') ORDER BY cid",
                        (name,),
                    ).fetchall()}
                    for name, sql in connection.execute(
                        "SELECT name, sql FROM sqlite_schema WHERE type='view' ORDER BY name"
                    )
                },
            }
        connection.close()
    result["sqlite_version"] = sqlite3.sqlite_version
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create synthetic, row-free demo snapshots.")
    parser.add_argument("directory")
    print(json.dumps(generate(parser.parse_args().directory), indent=2))
