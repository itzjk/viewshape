import hashlib
import os
import stat
import time
from pathlib import Path


class InvalidInput(Exception):
    pass


def fingerprint(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def check_sidecars(path):
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        try:
            if sidecar.stat().st_size:
                raise InvalidInput(f"Nonempty {suffix} sidecar: consolidate and close the snapshot first")
        except FileNotFoundError:
            pass


def copy_snapshot(source, destination, max_bytes, deadline):
    try:
        path = Path(source).expanduser().resolve(strict=True)
        check_sidecars(path)
        initial = path.stat()
        if not stat.S_ISREG(initial.st_mode):
            raise InvalidInput("Snapshot must be an existing regular file")
        if initial.st_size > max_bytes:
            raise InvalidInput(f"Snapshot exceeds max-bytes ({max_bytes})")
        digest = hashlib.sha256()
        with path.open("rb") as incoming, destination.open("xb") as outgoing:
            # Compare like-for-like. A descriptor opened on Windows reports different st_dev/st_ino
            # than a path stat of the same file, so an fd-vs-path fingerprint always looks "changed"
            # there. Guard the open against the path baseline on the identity-independent fields
            # (size and mtime), then track the open descriptor against its own first reading.
            opened = os.fstat(incoming.fileno())
            if (opened.st_size, opened.st_mtime_ns) != (initial.st_size, initial.st_mtime_ns):
                raise InvalidInput("Snapshot changed before copying")
            header = incoming.read(16)
            if header != b"SQLite format 3\x00":
                raise InvalidInput("Not a SQLite 3 database file")
            incoming.seek(0)
            size = 0
            while True:
                if time.monotonic() > deadline:
                    raise InvalidInput("Snapshot copy exceeded timeout")
                chunk = incoming.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes or size > initial.st_size:
                    raise InvalidInput("Snapshot grew during copying or exceeded max-bytes")
                outgoing.write(chunk)
                digest.update(chunk)
            if size != initial.st_size or fingerprint(os.fstat(incoming.fileno())) != fingerprint(opened):
                raise InvalidInput("Snapshot changed during copying")
            incoming.seek(0)
            verification = hashlib.sha256()
            while True:
                if time.monotonic() > deadline:
                    raise InvalidInput("Snapshot verification exceeded timeout")
                chunk = incoming.read(1024 * 1024)
                if not chunk:
                    break
                verification.update(chunk)
            if verification.digest() != digest.digest():
                raise InvalidInput("Snapshot content changed during verification")
            if fingerprint(os.fstat(incoming.fileno())) != fingerprint(opened):
                raise InvalidInput("Snapshot changed during verification")
        check_sidecars(path)
        if fingerprint(path.stat()) != fingerprint(initial):
            raise InvalidInput("Snapshot was replaced or changed during copying")
        return {"sha256": digest.hexdigest(), "bytes": size}
    except OSError as error:
        raise InvalidInput(str(error)) from error
