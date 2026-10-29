import json
import sqlite3
import sys
import time
from pathlib import Path


def emit(event):
    print(json.dumps(event, ensure_ascii=True), flush=True)


def classify(message, virtual_denied=False):
    lower = message.lower()
    if virtual_denied or "no such module:" in lower:
        return "unsupported"
    if any(text in lower for text in (
        "no such function:", "no such collation sequence:", "unsafe use of", "not authorized",
    )):
        return "environment-required"
    if any(text in lower for text in ("too big", "too many", "interrupted", "out of memory")):
        return "limit-exceeded"
    return "unresolvable"


def inspect(config):
    deadline = time.monotonic() + config["timeout"]
    connection = sqlite3.connect(Path(config["path"]).as_uri() + "?mode=ro", uri=True, timeout=0)
    try:
        try:
            connection.enable_load_extension(False)
        except AttributeError:
            pass
        for limit in (sqlite3.SQLITE_LIMIT_LENGTH, sqlite3.SQLITE_LIMIT_SQL_LENGTH):
            connection.setlimit(limit, min(max(config["max_bytes"], 262144), connection.getlimit(limit)))
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 2000)
        connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cache_size=-2048")
        if connection.execute("PRAGMA trusted_schema").fetchone() != (0,):
            raise sqlite3.NotSupportedError("trusted_schema=OFF is unavailable")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        total = connection.execute("SELECT count(*) FROM main.sqlite_schema").fetchone()[0]
        views_total = connection.execute("SELECT count(*) FROM main.sqlite_schema WHERE type='view'").fetchone()[0]
        emit({"event": "inventory", "objects_total": total, "views_total": views_total})
        if total > config["max_objects"]:
            emit({"event": "error", "category": "limit-exceeded", "message": "Schema exceeds max-objects"})
            return
        catalog = connection.execute("PRAGMA main.table_list").fetchall()
        virtual = {row[1] for row in catalog if row[2] == "virtual"}
        known_objects = {row[1] for row in catalog if row[0] == "main"} | {"sqlite_master", "sqlite_schema"}
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 262144)
        virtual_denied = [False]

        def authorize(action, first, second, database, origin):
            if action == sqlite3.SQLITE_READ:
                if database == "main" and (first in virtual or (origin is not None and first not in known_objects)):
                    virtual_denied[0] = True
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_PRAGMA and first == "table_xinfo":
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        connection.set_authorizer(authorize)
        rows = connection.execute(
            "SELECT name, sql FROM main.sqlite_schema WHERE type='view' ORDER BY name COLLATE BINARY"
        )
        used = 0
        for name, sql in rows:
            virtual_denied[0] = False
            result = {"event": "view", "name": name, "sql": sql, "columns": None, "status": "resolved"}
            try:
                if len(sql.encode("utf-8")) > 262144:
                    result["sql"] = None
                    result["sql_omitted"] = "exceeds 262144 bytes"
                    raise sqlite3.DataError("View definition is too big for the SQL limit")
                columns = connection.execute(
                    "SELECT cid, name, type FROM pragma_table_xinfo(?, 'main') ORDER BY cid", (name,)
                ).fetchall()
                quoted = '"' + name.replace('"', '""') + '"'
                connection.execute("EXPLAIN SELECT * FROM main." + quoted).close()
                if not columns:
                    raise sqlite3.OperationalError("No columns returned for an existing view")
                result["columns"] = [
                    {"position": cid, "name": column, "declared_type": declared_type}
                    for cid, column, declared_type in columns
                ]
            except sqlite3.Error as error:
                result["status"] = classify(str(error), virtual_denied[0])
                result["error"] = {"message": str(error), "sqlite_errorname": getattr(error, "sqlite_errorname", None)}
            used += len(json.dumps(result, ensure_ascii=True).encode("utf-8"))
            if used > config["max_output_bytes"] // 4:
                emit({"event": "error", "category": "limit-exceeded", "message": "Metadata exceeds output budget"})
                return
            emit(result)
        emit({"event": "complete"})
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        inspect(json.load(sys.stdin))
    except (sqlite3.Error, OSError, MemoryError) as error:
        emit({"event": "error", "category": classify(str(error)), "message": str(error)})
