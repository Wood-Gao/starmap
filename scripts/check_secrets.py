"""Check staged files or reachable Git history without printing matched values."""

import argparse
from pathlib import PurePosixPath
import re
import subprocess
import sys


PATTERNS = [
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(
        rb'''(?i)["']?(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)["']?'''
        rb'''\s*[:=]\s*["'][A-Za-z0-9_+/=.-]{16,}["']'''
    ),
]
FORBIDDEN = {"auth.json", "credentials.json", "secrets.json"}


def git(*args):
    return subprocess.check_output(["git", *args])


def suspicious(path, data):
    name = PurePosixPath(path).name
    forbidden = (
        name in FORBIDDEN
        or (name.startswith(".env") and name != ".env.example")
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    )
    return forbidden or any(pattern.search(data) for pattern in PATTERNS)


def entries(history):
    if history:
        for revision in git("rev-list", "--all").splitlines():
            for entry in git("ls-tree", "-r", "-z", revision.decode()).split(b"\0"):
                if entry:
                    metadata, path = entry.split(b"\t", 1)
                    _, kind, oid = metadata.split()
                    if kind == b"blob":
                        yield path, oid
    else:
        for entry in git("ls-files", "--stage", "-z").split(b"\0"):
            if entry:
                metadata, path = entry.split(b"\t", 1)
                mode, oid, stage = metadata.split()
                if stage != b"0":
                    raise ValueError("Unmerged index; resolve conflicts before scanning.")
                if mode != b"160000":
                    yield path, oid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    seen = set()
    count = 0
    blocked = False
    for raw_path, oid in entries(args.history):
        pair = (raw_path, oid)
        if pair in seen:
            continue
        seen.add(pair)
        count += 1
        path = raw_path.decode("utf-8", errors="replace")
        if suspicious(path, git("cat-file", "blob", oid.decode())):
            print("Potential credential or forbidden file: " + repr(path), file=sys.stderr)
            blocked = True
    if blocked:
        print("Publication blocked. Inspect locally; do not paste secret values.", file=sys.stderr)
        return 1
    print("Basic credential scan passed ({} file versions).".format(count))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (subprocess.CalledProcessError, ValueError) as exc:
        print("Credential scan failed: " + type(exc).__name__, file=sys.stderr)
        sys.exit(2)
