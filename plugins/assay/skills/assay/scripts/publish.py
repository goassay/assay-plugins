#!/usr/bin/env python3
"""
publish.py — the publishing half. Derive the work package from a directory and
put the task on the board.

    publish.py <package-dir> --scope src [--scope tests] \\
        --title "..." --spec SPEC.md \\
        --public tests/test_public.py \\
        --hidden .marketplace/hidden_tests/test_hidden.py \\
        [--regression .marketplace/regression_tests/test_regression.py] \\
        --budget 12000 --reserve 9000 \\
        [--bidding-hours 1] [--deadline-days 1] [--lease-minutes 60] [--attempts 3] \\
        [--yes]

Without --yes NOTHING IS SENT. The manifest — every path and its size — is
printed and the command stops, so what is about to leave this machine can be
read before it does. Publishing is irreversible; a worker who has read a
package cannot unread it.

E3 Revision 3, ruled 2026-09-07: the package is derived from a directory rather
than hand-listed. That is the better publishing experience and the larger
exposure in the same move, which is why the manifest is the thing to read, and
why three kinds of file never leave regardless of what the directory holds:

  * anything under a dotted path — `.marketplace/` holds the HIDDEN suites,
    `.git/` holds history, `.env` holds credentials;
  * any symlink, refused rather than skipped — a link to ~/.ssh inside the
    package would otherwise ship the key under an innocent name;
  * anything that is not UTF-8 text — a package is source, not assets.

`run.sh` must be at the root. The sandbox runs `sh ./run.sh` and reads a JUnit
report from /report; without it the verdict is RUN_FAILED and nothing says why.
The marketplace refuses that too — this checks first, so the refusal is local.
(This command publishes sandbox-judged tasks — `--hidden` is required — so the
runner always is. The buyer-judged task with nothing to run, E22, is published
from the plugin's `publish_task` or the web form, where a folder with no tests
needs no `run.sh`.)
"""
import argparse
import datetime as dt
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from assay import AssayError, call  # noqa: E402

# The server's caps (TaskService.MAX_FILE_CHARS / MAX_PACKAGE_CHARS), applied
# here first so a publisher learns about an oversized file before the request
# is built rather than from a 413 with the whole package in flight.
MAX_FILE_CHARS = 200_000
MAX_PACKAGE_CHARS = 1_000_000


def derive_package(root):
    """Every file under `root`, as {path, content}, relative and sorted.

    Raises AssayError for anything that must not leave; never skips silently.
    """
    root = pathlib.Path(root)
    if not root.is_dir():
        raise AssayError(f"{root} is not a directory.")

    files = []
    total = 0
    for parent, dirs, names in os.walk(root):
        # Prune in place so os.walk never descends. Sorted so the manifest is
        # stable across runs and machines.
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            path = pathlib.Path(parent) / name
            relative = path.relative_to(root).as_posix()

            if path.is_symlink():
                raise AssayError(
                    f"{relative} is a symlink and will not be followed.\n"
                    "A link inside the package can point anywhere on this machine; "
                    "copy the file in if it is meant to ship, or remove the link.")

            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise AssayError(
                    f"{relative} is not UTF-8 text and will not be sent.\n"
                    "A package is the scoped source a worker needs, not assets.") from None

            if len(content) > MAX_FILE_CHARS:
                raise AssayError(
                    f"{relative} is {len(content):,} characters; the limit is "
                    f"{MAX_FILE_CHARS:,} a file.")
            total += len(content)
            files.append({"path": relative, "content": content})

    if not files:
        raise AssayError(f"{root} holds nothing that can be sent.")
    if total > MAX_PACKAGE_CHARS:
        raise AssayError(
            f"The package is {total:,} characters; the limit is {MAX_PACKAGE_CHARS:,}.\n"
            "A package is the scoped source a worker needs, not a repository.")
    if not any(f["path"] == "run.sh" for f in files):
        raise AssayError(
            "The package has no run.sh at its root.\n"
            "The sandbox runs `sh ./run.sh` and reads a JUnit report from /report; "
            "without it every attempt fails as RUN_FAILED.")
    return files


def manifest(files):
    """What is about to leave, one line a file, for a person to read."""
    lines = [f"  {len(f['content']):>9,}  {f['path']}" for f in files]
    total = sum(len(f["content"]) for f in files)
    lines.append(f"  {total:>9,}  ({len(files)} files)")
    return "\n".join(lines)


def _iso(when):
    return when.isoformat().replace("+00:00", "Z")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("package_dir")
    ap.add_argument("--scope", action="append", required=True,
                    help="a path prefix the worker may change; repeat for more")
    ap.add_argument("--title", required=True)
    ap.add_argument("--spec", required=True, help="SPEC.md")
    ap.add_argument("--public", required=True, help="the public suite file")
    ap.add_argument("--hidden", required=True, help="the hidden suite file")
    ap.add_argument("--regression", help="the regression suite file")
    ap.add_argument("--budget", type=int, required=True)
    ap.add_argument("--reserve", type=int, required=True)
    ap.add_argument("--bidding-hours", type=float, default=1)
    ap.add_argument("--deadline-days", type=float, default=1)
    ap.add_argument("--lease-minutes", type=int, default=60)
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--yes", action="store_true",
                    help="actually send it; without this only the manifest is printed")
    args = ap.parse_args(argv)

    files = derive_package(args.package_dir)
    read = lambda p: pathlib.Path(p).read_text(encoding="utf-8")  # noqa: E731

    print("This would leave the machine, as the work package:")
    print(manifest(files))
    print(f"Scope: {', '.join(args.scope)}")
    print(f"Held back for the verifier: {args.hidden}"
          + (f", {args.regression}" if args.regression else ""))
    if not args.yes:
        print("\nNothing sent. Read the list; re-run with --yes to publish.")
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    body = {
        "title": args.title,
        "specMarkdown": read(args.spec),
        "maxBudget": args.budget,
        "reservePrice": args.reserve,
        "biddingClosesAt": _iso(now + dt.timedelta(hours=args.bidding_hours)),
        "totalDeadline": _iso(now + dt.timedelta(days=args.deadline_days)),
        "leaseSeconds": args.lease_minutes * 60,
        "maxAttempts": args.attempts,
        "publicSuite": read(args.public),
        "hiddenSuite": read(args.hidden),
        "regressionSuite": read(args.regression) if args.regression else "",
        "workPackage": files,
        "inScopePaths": args.scope,
    }
    published = call("POST", "/v1/tasks", body)
    print(f"\nPublished {published['id']}. The budget is escrowed; the package is "
          "frozen; the hidden and regression suites are now unreadable, including by you.")
    return 0


if __name__ == "__main__":
    try:
        # No load_key() here, unlike work.py: the dry run and --help must work
        # for someone who has no credential yet. call() reads the key when, and
        # only when, --yes sends the request.
        sys.exit(main(sys.argv[1:]))
    except AssayError as problem:
        print(f"\n{problem}\n", file=sys.stderr)
        sys.exit(1)
