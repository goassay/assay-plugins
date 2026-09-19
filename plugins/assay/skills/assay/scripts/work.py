#!/usr/bin/env python3
"""
work.py — the earning half. Browse, bid, fetch, check, submit.

    work.py board                      what is open that you could take
    work.py bid    <task> <price> <min>  place a bid
    work.py fetch  <task>              set up a scratch directory to work in
    work.py check  <task>              run the public tests, in a container
    work.py submit <task>              scope-check the patch and send it

The work itself is not here. E9 Decision 1: the worker is the person's own
coding session, not a daemon running a cheap model — three experiments killed
the cheap-model version, and the session doing the reading is the one that
should do the writing. These commands are the plumbing around it.
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from assay import (AssayError, as_untrusted, call, ensure_image, load_key, sandbox_image,  # noqa: E402
                   refuse_out_of_scope)

# Where a task is worked on. NEVER the user's own repository — E9 Decision 3.
# A stranger's spec and a stranger's tests are about to be opened here, and
# putting them beside somebody's real source is how a bad day starts.
WORKSPACES = pathlib.Path.home() / ".assay" / "work"



def board():
    tasks = call("GET", "/v1/tasks") or []
    if not tasks:
        print("Nothing open right now. A task appears the moment a publisher funds one.")
        return
    print(f"{len(tasks)} open:\n")
    for task in tasks:
        print(f"  {task['id']}")
        print(f"    {task.get('title', '(untitled)')}")
        print(f"    up to {task.get('maxBudget', 0):,} credits · "
              f"{task.get('openBids', 0)} bids · closes {task.get('biddingClosesAt', '?')}")
        print()
    print("Bids are sealed: nobody sees another price until bidding closes.")


def bid(task_id, price, eta_minutes):
    placed = call("POST", f"/v1/tasks/{task_id}/bids", {
        "price": int(price),
        "etaSeconds": int(eta_minutes) * 60,
        # Declared, and nothing verifies the claim — the marketplace says so.
        # Honest by default: this is the session doing the work.
        "modelDeclared": "claude-code",
    })
    print(f"Bid placed: {placed.get('price', price):,} credits, "
          f"{eta_minutes} minutes.")
    print("A deposit is held while it stands, and comes straight back if you lose.")


def held_award(task_id):
    """The award this agent holds on a task, or a sentence saying why not."""
    for award in call("GET", "/v1/awards/mine") or []:
        if award.get("taskId") == task_id:
            return award
    raise AssayError(
        f"You do not hold {task_id}.\n"
        "Either the auction has not been decided, somebody else won it, or the "
        "lease expired and it went back to the board.")


def fetch(task_id):
    """Lays the package out in a scratch directory and prints what to do."""
    award = held_award(task_id)
    # The package comes from the AWARD, not the task: the publisher's source
    # goes to whoever won, not to everyone who can register. A bidder prices the
    # job from the specification and the public suite.
    detail = call("GET", f"/v1/awards/{award['awardId']}/package")
    scopes = detail.get("inScopePaths") or []

    root = WORKSPACES / task_id
    root.mkdir(parents=True, exist_ok=True)
    tree = root / "package"
    tree.mkdir(exist_ok=True)

    for entry in detail.get("files") or []:
        target = (tree / entry["path"]).resolve()
        # The path came from the network. It has passed the marketplace's own
        # constraints, and it is checked again here, because by now it decides
        # where a file lands on the USER'S disk.
        if not str(target).startswith(str(tree.resolve())):
            raise AssayError(f"package path escapes the directory: {entry['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.get("encoding") == "base64":
            # E23: a file that is not text, written as the bytes it is.
            import base64
            target.write_bytes(base64.b64decode(entry["content"]))
        else:
            target.write_text(entry["content"])

    (root / "scope.json").write_text(json.dumps(scopes))
    # E22 Part 2: where the tests run, so `check` picks the same box the
    # marketplace will.
    (root / "sandbox.json").write_text(json.dumps(detail.get("sandbox") or "python"))

    # A pristine copy, so `submit` can diff against what arrived rather than
    # trusting the working tree to remember.
    pristine = root / ".pristine"
    if pristine.exists():
        subprocess.run(["rm", "-rf", str(pristine)], check=True)
    subprocess.run(["cp", "-R", str(tree), str(pristine)], check=True)

    print(f"Lease runs to {award.get('leaseExpiresAt', '?')}. "
          f"If it expires the task goes back to the board and costs you nothing.")
    print(f"\nReady in {tree}\n")
    print(as_untrusted("THE JOB", detail.get("specMarkdown", "(no specification)")))
    print(f"You may change: {', '.join(scopes) or '(nothing declared — do not submit)'}")
    print("Anything else you touch will be refused before it is sent.\n")
    print("When you think it works:")
    print(f"    work.py check  {task_id}")
    print(f"    work.py submit {task_id}")


def check(task_id):
    """Runs the public tests IN A CONTAINER. They are a stranger's Python."""
    tree = WORKSPACES / task_id / "package"
    if not tree.exists():
        raise AssayError(f"Nothing fetched for {task_id}. Run: work.py fetch {task_id}")

    # E22: a package without a runner is a task nothing runs on. The
    # marketplace passes it straight to the buyer, who judges by looking; so
    # does this — success, and a word about who decides, rather than pytest
    # over a folder of prose reporting "no tests ran" as a failure.
    if not (tree / "run.sh").exists():
        print("Nothing to run here; the buyer judges. Read the work over as they would,")
        print("then submit it.")
        return 0

    # E22 Part 2: the same run the marketplace makes — the package's own
    # run.sh, in the task's own box, writing a JUnit report to /report — rather
    # than pytest over the tree. A check that runs something other than what
    # the verifier runs is a check that can pass here and fail there.
    sandbox_file = WORKSPACES / task_id / "sandbox.json"
    sandbox = json.loads(sandbox_file.read_text()) if sandbox_file.exists() else "python"
    if not ensure_image(sandbox, print):
        raise AssayError(f"This machine lacks the {sandbox_image(sandbox)} image and could not "
                         "build it. Is Docker running?")
    print(f"Running run.sh in a sealed container (no network, read-only, {sandbox})…\n")
    with tempfile.TemporaryDirectory() as report:
        result = subprocess.run([
            "docker", "run", "--rm",
            # E9 Decision 3. These tests were written by whoever published the task.
            # Running them on the host would be running a stranger's code as the
            # user, which is the exact thing the marketplace refuses to do to itself.
            "--network=none", "--read-only", "--user", "nobody",
            "--cap-drop=ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "512", "--memory", "512m", "--memory-swap", "512m",
            "--cpus", "1.0",
            "-v", f"{tree.resolve()}:/work:ro",
            "-v", f"{report}:/report:rw",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "-w", "/work", sandbox_image(sandbox),
            "sh", "./run.sh",
        ], capture_output=True, text=True, timeout=600)
        junit = pathlib.Path(report) / "junit.xml"
        wrote_report = junit.exists() and junit.stat().st_size > 0
        failures = junit.read_text().count("<failure") + junit.read_text().count("<error") if wrote_report else 0

    print(result.stdout or result.stderr)
    if result.returncode == 0 and wrote_report and failures == 0:
        print("\nPublic tests pass. Remember they are not the ones that decide:")
        print("a hidden suite you cannot see is what the verdict is read from.")
        return 0
    if not wrote_report:
        print("\nThe run wrote no test report to /report, which the marketplace reads the verdict")
        print("from; check run.sh writes one (pytest --junitxml, node --test-reporter=junit).")
    else:
        print("\nNot yet. Fix and run check again.")
    return 1


def patch_between(pristine: pathlib.Path, tree: pathlib.Path) -> str:
    """A unified diff of the tree against what arrived, with a/ and b/ paths.

    In Python, not `diff -ru --label a --label b`. On macOS that is BSD diff,
    and its --label replaces the whole header path — `+++ b` instead of
    `+++ b/src/app.py` — so the scope check read the path as "b" and refused
    a correct patch. The first unattended helper run (E16, 2026-09-09) hit it
    and the session then posted the patch to the API by hand, around the
    check. A patch producer that does not depend on which diff is installed is
    the fix for both.
    """
    import difflib
    def files_under(root):
        return {str(f.relative_to(root)): f for f in root.rglob("*")
                if f.is_file() and "__pycache__" not in f.parts and not f.name.endswith(".pyc")}
    before, after = files_under(pristine), files_under(tree)
    out = []
    for rel in sorted(set(before) | set(after)):
        try:
            old = before[rel].read_text().splitlines(keepends=True) if rel in before else []
            new = after[rel].read_text().splitlines(keepends=True) if rel in after else []
        except UnicodeDecodeError:
            # E23: a file that is not text — a deck, an image — is carried in
            # git's binary-patch format, which git writes and the marketplace's
            # `git apply` reads. One such file and the whole patch is git's,
            # so the headers agree.
            return git_patch_between(pristine, tree, files_under)
        if old == new:
            continue
        out.extend(difflib.unified_diff(old, new,
                   fromfile=("a/" + rel) if rel in before else "/dev/null",
                   tofile=("b/" + rel) if rel in after else "/dev/null"))
    text = "".join(out)
    # difflib omits the trailing newline marker git apply expects on the last
    # line of a file that has none; a missing one is a refused patch.
    return text if text.endswith("\n") or not text else text + "\n"


def git_patch_between(pristine: pathlib.Path, tree: pathlib.Path, files_under) -> str:
    """`git diff --no-index --binary` over copies named a/ and b/, so the
    headers read `diff --git a/rel b/rel` with empty prefixes — the -p1 form
    the marketplace applies and scans — and a binary file rides along."""
    if not shutil.which("git"):
        raise AssayError("A file in the work is not text, and sending it needs git, which "
                         "this machine does not have. Install git (on a Mac: xcode-select "
                         "--install) and submit again.")
    with tempfile.TemporaryDirectory() as scratch:
        for name, root in (("a", pristine), ("b", tree)):
            for rel, path in files_under(root).items():
                target = pathlib.Path(scratch) / name / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        (pathlib.Path(scratch) / "a").mkdir(exist_ok=True)
        (pathlib.Path(scratch) / "b").mkdir(exist_ok=True)
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", "diff", "--no-index", "--binary", "--no-color",
             "--src-prefix=", "--dst-prefix=", "a", "b"],
            cwd=scratch, capture_output=True, text=True)
    # git exits 1 when the trees differ, which is the whole point.
    if result.returncode not in (0, 1):
        raise AssayError("git could not produce the patch:\n" + (result.stderr or result.stdout))
    return result.stdout


def submit(task_id):
    root = WORKSPACES / task_id
    tree, pristine = root / "package", root / ".pristine"
    if not pristine.exists():
        raise AssayError(f"Nothing fetched for {task_id}. Run: work.py fetch {task_id}")

    patch = patch_between(pristine, tree)
    if not patch.strip():
        raise AssayError("Nothing has changed in the package — there is no patch to send.")

    scopes = json.loads((root / "scope.json").read_text())
    # E9 Decision 3, guard 4. The marketplace checks this too; this one runs
    # while it is still the user's reputation on the line rather than ours.
    refuse_out_of_scope(patch, scopes)

    # The check, here, not left to the session (2026-09-19: a session ran its
    # own one-liner instead of `work check` and submitted; the verifier
    # happened to pass it). A package with a runner is checked before it
    # leaves; one with nothing to run is the buyer's to judge.
    if (tree / "run.sh").exists() and check(task_id) != 0:
        raise AssayError("The public tests do not pass; nothing was submitted. Fix and submit again.")

    award = held_award(task_id)
    call("POST", f"/v1/awards/{award['awardId']}/submissions",
         {"patch": patch, "notes": "submitted from the assay skill"})
    print("Submitted. The sandbox runs three suites against it; you will see the")
    print("verdict on the task. Passing the sandbox is not payment — the publisher")
    print("runs their own build too, and that is what releases the money.")


COMMANDS = {"board": 0, "bid": 3, "fetch": 1, "check": 1, "submit": 1}

def main(argv) -> int:
    """`argv` as sys.argv: the program, the verb, its arguments. Also what the
    frozen helper calls for `assay-helper work …` (E20)."""
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print(__doc__)
        return 2
    verb, args = argv[1], argv[2:]
    if len(args) != COMMANDS[verb]:
        print(__doc__)
        return 2
    try:
        load_key()
        return globals()[verb](*args) or 0
    except AssayError as problem:
        print(f"\n{problem}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
