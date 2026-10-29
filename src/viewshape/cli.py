import argparse
import json
import math
import sys

from . import __version__
from .compare import Limits, compare


def positive_integer(text):
    value = int(text)
    if value <= 0 or value > 9223372036854775807:
        raise argparse.ArgumentTypeError("must be between 1 and 9223372036854775807")
    return value


def positive_seconds(text):
    value = float(text)
    if not math.isfinite(value) or value <= 0 or value > 300:
        raise argparse.ArgumentTypeError("must be finite, positive, and at most 300 seconds")
    return value


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(3, f"viewshape: error: {message}\n")


def literal(value):
    return json.dumps(value, ensure_ascii=True)


def human(report):
    lines = [f"viewshape | SQLite {report['sqlite_version']} | {report['status']}"]
    limits = report["limits"]
    lines.append(f"limits: {limits['max_bytes']} input bytes, {limits['max_objects']} objects, "
                 f"{limits['timeout']:g}s per stage, {limits['max_output_bytes']} report bytes")
    for side, snapshot in report["snapshots"].items():
        if "coverage" in snapshot:
            count = snapshot["coverage"]
            lines.append(f"{side}: {count['views_resolved']}/{count['views_total']} views resolved, "
                         f"{count['views_failed']} failed, {count['views_omitted']} omitted")
    for view in report["views"]:
        lines.append(f"\n{literal(view['name'])}: {view['status']}")
        for side in ("before", "after"):
            value = view[side]
            if value is None:
                lines.append(f"  {side}: absent or not inspected (see coverage)")
                continue
            lines.append(f"  {side} SQL: {literal(value['sql'])}")
            if value["status"] != "resolved":
                lines.append(f"  {side}: {value['status']}: {literal(value['error']['message'])}")
            else:
                columns = ", ".join(f"{c['position']}:{literal(c['name'])} {literal(c['declared_type'])}" for c in value["columns"])
                lines.append(f"  {side} columns: {columns}")
        for change in view["changes"]:
            kind = change["kind"]
            if kind in ("column-added", "column-removed"):
                column = change["after"] if kind == "column-added" else change["before"]
                symbol = "+" if kind == "column-added" else "-"
                lines.append(f"  {symbol} column[{column['position']}] {literal(column['name'])} {literal(column['declared_type'])}")
            else:
                lines.append(f"  {kind} {literal(change['name'])}: {literal(change['before'])} -> {literal(change['after'])}")
    if report.get("report_views_omitted"):
        lines.append(f"Report details omitted for {report['report_views_omitted']} views due to output limit.")
    if report["empty"]:
        lines.append("Empty comparison: neither snapshot has main views; migration compatibility is not assessed.")
    for error in report["errors"]:
        lines.append(f"{error.get('side', 'comparison')}: {error['category']}: {literal(error['message'])}")
    lines.append("Metadata only; application compatibility and SELECT results are not assessed.")
    return "\n".join(lines) + "\n"


def render(report, as_json, max_bytes):
    output = json.dumps(report, ensure_ascii=True, indent=2) + "\n" if as_json else human(report)
    if len(output.encode("utf-8")) <= max_bytes:
        return output, report["exit_code"]
    reduced = dict(report)
    code = 3 if report["exit_code"] == 3 else 2
    reduced.update(status="invalid-input" if code == 3 else "incomplete", exit_code=code, views=[])
    reduced["report_views_omitted"] = len(report["views"])
    reduced["errors"] = [{"category": "limit-exceeded", "message": "Rendered report exceeds max-output-bytes; view details omitted"}]
    output = json.dumps(reduced, ensure_ascii=True) + "\n" if as_json else human(reduced)
    return output, code


def main(argv=None):
    parser = Parser(description="Compare resolved SQLite view columns in two closed snapshots.")
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--json", action="store_true", help="emit format_version 1 JSON")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--max-bytes", type=positive_integer, default=Limits.max_bytes, help="maximum bytes per input (default: 268435456)")
    parser.add_argument("--max-objects", type=positive_integer, default=Limits.max_objects, help="maximum schema objects per snapshot (default: 1000)")
    parser.add_argument("--timeout", type=positive_seconds, default=Limits.timeout, help="seconds per copy or inspection, at most 300 (default: 10)")
    parser.add_argument("--max-output-bytes", type=positive_integer, default=Limits.max_output_bytes, help="maximum report bytes, at least 4096 (default: 2097152)")
    args = parser.parse_args(argv)
    if args.max_output_bytes < 4096:
        parser.error("--max-output-bytes must be at least 4096")
    limits = Limits(args.max_bytes, args.max_objects, args.timeout, args.max_output_bytes)
    report = compare(args.before, args.after, limits)
    output, code = render(report, args.json, limits.max_output_bytes)
    try:
        sys.stdout.write(output)
    except BrokenPipeError:
        return code
    return code


if __name__ == "__main__":
    sys.exit(main())
