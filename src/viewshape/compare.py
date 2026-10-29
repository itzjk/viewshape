import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import __version__
from .snapshot import InvalidInput, copy_snapshot


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 268435456
    max_objects: int = 1000
    timeout: float = 10.0
    max_output_bytes: int = 2097152


MIN_SQLITE = (3, 37, 0)


def inspect_snapshot(path, limits, directory, side):
    config = {**asdict(limits), "path": str(path)}
    events_path = directory / (side + ".jsonl")
    timed_out = False
    with events_path.open("wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-I", "-m", "viewshape._worker"], stdin=subprocess.PIPE,
            stdout=output, stderr=subprocess.DEVNULL,
        )
        try:
            process.communicate(json.dumps(config).encode(), timeout=limits.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.communicate()
    result = {"objects_total": None, "views_total": None, "views": {}, "errors": [], "complete": False}
    with events_path.open() as events:
        for line in events:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                break
            kind = event.pop("event")
            if kind == "inventory":
                result.update(event)
            elif kind == "view":
                result["views"][event.pop("name")] = event
            elif kind == "error":
                result["errors"].append(event)
            elif kind == "complete":
                result["complete"] = True
    if timed_out or (not result["complete"] and not result["errors"]):
        result["errors"].append({
            "category": "limit-exceeded" if timed_out else "unresolvable",
            "message": "Inspection timed out" if timed_out else "Inspection worker stopped before completion",
        })
    return result


def column_changes(before, after):
    old = {column["name"]: column for column in before}
    new = {column["name"]: column for column in after}
    changes = []
    for column in before:
        name = column["name"]
        if name not in new:
            changes.append({"kind": "column-removed", "before": column})
        else:
            other = new[name]
            if column["position"] != other["position"]:
                changes.append({"kind": "position-changed", "name": name,
                                "before": column["position"], "after": other["position"]})
            if column["declared_type"] != other["declared_type"]:
                changes.append({"kind": "declared-type-changed", "name": name,
                                "before": column["declared_type"], "after": other["declared_type"]})
    for column in after:
        if column["name"] not in old:
            changes.append({"kind": "column-added", "after": column})
    return changes


def coverage(snapshot):
    views = snapshot["views"]
    resolved = sum(view["status"] == "resolved" for view in views.values())
    total = snapshot["views_total"]
    return {
        "objects_total": snapshot["objects_total"], "views_total": total,
        "views_inspected": len(views), "views_resolved": resolved,
        "views_failed": len(views) - resolved,
        "views_omitted": None if total is None else total - len(views),
        "inventory_complete": snapshot["complete"],
    }


def compare(before, after, limits=Limits()):
    report = {
        "format_version": 1, "tool_version": __version__, "sqlite_version": sqlite3.sqlite_version,
        "scope": "main views; ordered names and declared types; no row or SELECT semantics comparison",
        "limits": {**asdict(limits), "max_columns": 2000, "max_view_sql_bytes": 262144},
        "status": "invalid-input", "exit_code": 3, "differences_observed": False,
        "empty": False, "snapshots": {}, "views": [], "errors": [],
    }
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        report.update(status="incomplete", exit_code=2)
        report["errors"].append({"category": "unsupported", "message": "SQLite 3.37.0 or newer is required"})
        return report
    try:
        with tempfile.TemporaryDirectory(prefix="viewshape-") as temporary:
            directory = Path(temporary)
            for side, original in (("before", before), ("after", after)):
                private = directory / (side + ".sqlite")
                try:
                    report["snapshots"][side] = copy_snapshot(
                        original, private, limits.max_bytes, time.monotonic() + limits.timeout,
                    )
                except InvalidInput as error:
                    report["errors"].append({"side": side, "category": "invalid-input", "message": str(error)})
                    return report
            snapshots = {
                side: inspect_snapshot(directory / (side + ".sqlite"), limits, directory, side)
                for side in ("before", "after")
            }
            incomplete = False
            for side, snapshot in snapshots.items():
                report["snapshots"][side]["coverage"] = coverage(snapshot)
                report["errors"].extend({"side": side, **error} for error in snapshot["errors"])
                incomplete |= not snapshot["complete"]
            old, new = snapshots["before"], snapshots["after"]
            for name in sorted(old["views"].keys() | new["views"].keys()):
                left, right = old["views"].get(name), new["views"].get(name)
                definition_changed = (left["sql"] != right["sql"]
                                      if left and right and left["sql"] is not None and right["sql"] is not None else None)
                entry = {"name": name, "before": left, "after": right, "changes": [],
                         "definition_changed": definition_changed, "output_changed": None}
                absent = new if left else old
                if (left is None or right is None) and absent["complete"]:
                    entry["status"] = "view-added" if left is None else "view-removed"
                    report["differences_observed"] = True
                    if (right or left)["status"] != "resolved":
                        incomplete = True
                elif left is None or right is None or left["status"] != "resolved" or right["status"] != "resolved":
                    entry["status"] = "unverified"
                    incomplete = True
                    if definition_changed:
                        report["differences_observed"] = True
                else:
                    entry["output_changed"] = left["columns"] != right["columns"]
                    entry["changes"] = column_changes(left["columns"], right["columns"])
                    if definition_changed or entry["output_changed"]:
                        entry["status"] = (
                            ("definition-changed" if definition_changed else "definition-unchanged") + "/" +
                            ("output-changed" if entry["output_changed"] else "output-unchanged")
                        )
                        report["differences_observed"] = True
                    else:
                        entry["status"] = "unchanged"
                report["views"].append(entry)
            report["empty"] = old["views_total"] == new["views_total"] == 0 and not incomplete
            report["status"] = "incomplete" if incomplete else "differences" if report["differences_observed"] else "empty" if report["empty"] else "no-differences-observed"
            report["exit_code"] = 2 if incomplete else 1 if report["differences_observed"] else 0
    except OSError as error:
        report["errors"].append({"category": "invalid-input", "message": str(error)})
    return report
