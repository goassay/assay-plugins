#!/usr/bin/env python3
"""
helper.py — the something that runs the helper (E16).

    helper.py            run until stopped: look for work every minute
    helper.py once       one pass, then exit (what a cron or a test calls)

A skill cannot wake itself. This is the loop that does: every INTERVAL it asks
Assay whether there is a job this helper should go for or an award it holds,
and only then starts the person's own coding session headlessly — `claude -p`
with `helper-prompt.md` — to do the part that needs judgement. Between jobs it
sleeps. It never holds anything but the Assay key the skill already uses.

What the loop decides itself, because Assay's policy is Assay's (E16, ruled
2026-09-09) and money must not depend on a model's mood:
  * the price: the reserve if there is one, otherwise 75 % of the budget;
  * the ETA: the lease;
  * whether the machine can even run the package's tests (python, pytest and
    docker — `work.py check` runs a stranger's tests in a container and nowhere
    else), before the model is asked anything;
  * whether bidding is still open — the board lists tasks whose window has
    closed, and a bid on one is a 409 and a wasted turn.

What the model decides: whether it can do the job, and the job itself.

Every headless session runs in ~/.assay/work, never the person's repository
(E9 Decision 3), with no MCP servers and only the file tools and Bash.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from assay import AssayError, as_untrusted, call, load_key  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
# Frozen (E20): PyInstaller unpacks the bundle under sys._MEIPASS and the one
# binary is both the loop and `work`; a checkout runs the scripts as files.
FROZEN = bool(getattr(sys, "frozen", False))
PROMPT = (pathlib.Path(getattr(sys, "_MEIPASS", HERE)) / "helper-prompt.md") if FROZEN \
    else HERE.parent / "helper-prompt.md"
# How the session is told to run the plumbing. Frozen, there is no python3
# on the machine to speak of: the binary itself answers `work …`.
WORK_COMMAND = f"{sys.executable} work" if FROZEN else f"python3 {HERE / 'work.py'}"
WORK = pathlib.Path(os.environ.get("ASSAY_WORK", str(pathlib.Path.home() / ".assay" / "work")))
STATE = WORK.parent / "helper-state.json"
INTERVAL = int(os.environ.get("ASSAY_HELPER_INTERVAL", "60"))
CLAUDE = os.environ.get("ASSAY_CLAUDE", "claude")
CODEX = os.environ.get("ASSAY_CODEX", "codex")


def session_runner() -> str:
    """Which coding session does the work: "claude" or "codex" (E16 Revision 3,
    2026-09-16 — the owner: "let's add codex support as well").

    ASSAY_SESSION says; otherwise the first one on the machine, Claude Code
    first. The choice is a machine fact, not a preference: a person has the
    one they pay for."""
    chosen = os.environ.get("ASSAY_SESSION", "").strip().lower()
    if chosen in ("claude", "codex"):
        return chosen
    if shutil.which(CLAUDE):
        return "claude"
    if shutil.which(CODEX):
        return "codex"
    return "claude"


def model_declared() -> str:
    """What the bid says it runs on. A claim nobody verifies, so it is at least
    honest about which product is behind it."""
    return "codex" if session_runner() == "codex" else "claude-code"


SANDBOX_IMAGE = "assay-verifier:1"
# How many headless sessions an award gets before the loop leaves it alone.
# The first unattended run (2026-09-09) started a session every pass on an
# award whose check could not run, five times in eight minutes, each one
# reaching the same STOPPED — a lease's worth of somebody's quota for nothing.
ATTEMPTS_PER_AWARD = 2


# ── decisions the loop makes itself ─────────────────────────────────────────

def price_for(task) -> int:
    """Assay's policy: the reserve if there is one, else three quarters of the budget."""
    reserve = int(task.get("reservePrice") or 0)
    if reserve > 0:
        return reserve
    return int(task.get("maxBudget", 0)) * 3 // 4


def eta_for(task) -> int:
    """The lease, in seconds — promise what the task allows, no more, no less."""
    return int(task.get("leaseSeconds") or 1800)


def bidding_open(task, now: datetime) -> bool:
    """The board lists tasks whose window has closed; the loop does not bid on them."""
    closes = task.get("biddingClosesAt")
    if not closes:
        return False
    return datetime.fromisoformat(closes.replace("Z", "+00:00")) > now


def looks_like_python(public_suite: str) -> bool:
    """The only toolchain this version can run. Anything else is skipped, not guessed at."""
    text = public_suite or ""
    return ("def test_" in text or "import pytest" in text) and "package main" not in text


def toolchain_ready() -> list:
    """What is missing on this machine for a Python package, in words; empty means ready."""
    missing = []
    # A checkout needs the python that runs work.py; the frozen binary is its
    # own python, so the machine's is not asked about (E20).
    if not FROZEN:
        if shutil.which("python3") is None:
            missing.append("python3")
        else:
            probe = subprocess.run([shutil.which("python3"), "-c", "import pytest"],
                                   capture_output=True, text=True)
            if probe.returncode != 0:
                missing.append("pytest (python3 -m pip install pytest)")
    if shutil.which("docker") is None:
        missing.append("docker (work.py check runs the tests in a container)")
    elif not sandbox_image_present():
        # Found by the first unattended run: docker was there, the image was
        # not, and the session — correctly — refused to submit unchecked work,
        # but only after the award was held. The image is part of "this
        # machine can run the tests", so it is asked about before any bid.
        missing.append(f"the {SANDBOX_IMAGE} image (docker build -t {SANDBOX_IMAGE} verifier/)")
    if shutil.which(CLAUDE) is None and shutil.which(CODEX) is None:
        missing.append(f"{CLAUDE} or {CODEX} (the coding session that does the work)")
    return missing


def sandbox_image_present() -> bool:
    probe = subprocess.run([shutil.which("docker") or "docker", "image", "inspect", SANDBOX_IMAGE],
                           capture_output=True, text=True)
    return probe.returncode == 0


def worth_asking(task, now: datetime, already_bid: set) -> bool:
    """Open, still accepting bids, not already bid on by this helper."""
    return (task.get("status") == "OPEN"
            and task.get("id") not in already_bid
            and bidding_open(task, now))


def awards_needing_work(awards, submitted: set, attempts: dict = None) -> list:
    """Held, still active, not yet submitted, and not already tried ATTEMPTS_PER_AWARD times."""
    attempts = attempts or {}
    return [a for a in awards or []
            if a.get("status") == "ACTIVE" and a.get("taskId") not in submitted
            and attempts.get(a.get("taskId"), 0) < ATTEMPTS_PER_AWARD]


# ── state: what this helper already did, so a pass is idempotent ────────────

def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except json.JSONDecodeError:
            pass
    return {"bid": [], "submitted": [], "attempts": {}}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))


# ── the headless session ────────────────────────────────────────────────────

COSTS = WORK.parent / "helper-costs.jsonl"


def ask_claude(prompt: str, timeout: int) -> str:
    """One headless session: file tools and Bash, no MCP, in the scratch root.

    Returns the session's report. What it COST is kept beside it: the session
    is asked for JSON, which carries the tokens and the price, and
    `SESSION["usage"]` holds them for the caller to record. The owner asked on
    2026-09-09 whether anything counted the tokens a job used; nothing did,
    and the number is not small — a session that only says "OK" reports about
    30,000 tokens, because every headless run pays for its own context.
    """
    WORK.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    if session_runner() == "codex":
        # Codex's headless mode. The same trust as the Claude line below —
        # dontAsk with Bash is a session that runs what it decides, and so is
        # this flag; the guard is E9 Decision 3's scratch root and work.py's
        # container, not the session's own sandbox, which would also block
        # the docker socket `work check` needs. --ignore-user-config: no MCP
        # servers, no project rules — the session gets the prompt and the
        # tools, as Claude does with an empty --mcp-config.
        result = subprocess.run(
            [CODEX, "exec", "--json", "--ignore-user-config", "--skip-git-repo-check",
             "--ephemeral", "--dangerously-bypass-approvals-and-sandbox", "-C", str(WORK), prompt],
            cwd=WORK, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        text, usage = parse_codex_session(result.stdout or "")
    else:
        result = subprocess.run(
            [CLAUDE, "-p", prompt, "--output-format", "json",
             "--permission-mode", "dontAsk",
             "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep",
             "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"],
            cwd=WORK, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        text, usage = parse_session(result.stdout or "")
    if usage and not usage.get("duration_ms"):
        usage["duration_ms"] = int((time.monotonic() - started) * 1000)
    SESSION["usage"] = usage
    return text + (("\n" + result.stderr) if result.returncode != 0 else "")


# The last session's bill, for the caller to record. A module dict rather
# than an attribute on the function, so a test can stand a lambda in for
# ask_claude and still say what it cost.
SESSION: dict = {"usage": {}}


def parse_session(stdout: str):
    """The report and the bill out of a JSON session; a bare transcript if it is not JSON."""
    try:
        d = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return stdout, {}
    u = d.get("usage") or {}
    usage = {
        "input_tokens": int(u.get("input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
        "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
        "cache_creation_tokens": int(u.get("cache_creation_input_tokens") or 0),
        "cost_usd": float(d.get("total_cost_usd") or 0.0),
        "duration_ms": int(d.get("duration_ms") or 0),
        "turns": int(d.get("num_turns") or 0),
    }
    usage["total_tokens"] = (usage["input_tokens"] + usage["output_tokens"]
                             + usage["cache_read_tokens"] + usage["cache_creation_tokens"])
    return str(d.get("result") or ""), usage


def parse_codex_session(stdout: str):
    """The report and the bill out of `codex exec --json`: one JSON event per
    line; the last agent_message is the report, turn.completed carries the
    tokens. Codex on a ChatGPT plan has no price per token, so cost_usd is 0
    and the tokens are what is recorded. An error event is reported in its
    own words, so a stopped session says why."""
    text, usage, errors, turns = "", {}, [], 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        kind = d.get("type")
        item = d.get("item") or {}
        if kind == "item.completed" and item.get("type") == "agent_message":
            text = str(item.get("text") or "")
        elif kind == "item.completed" and item.get("type") == "error":
            errors.append(str(item.get("message") or ""))
        elif kind in ("error", "turn.failed"):
            errors.append(str((d.get("error") or {}).get("message") or d.get("message") or ""))
        elif kind == "turn.completed":
            turns += 1
            u = d.get("usage") or {}
            usage = {
                "input_tokens": int(u.get("input_tokens") or 0),
                "output_tokens": int(u.get("output_tokens") or 0),
                "cache_read_tokens": int(u.get("cached_input_tokens") or 0),
                "cache_creation_tokens": int(u.get("cache_write_input_tokens") or 0),
                "cost_usd": 0.0,
                "duration_ms": 0,
                "turns": turns,
            }
            usage["total_tokens"] = (usage["input_tokens"] + usage["output_tokens"]
                                     + usage["cache_read_tokens"] + usage["cache_creation_tokens"])
    if errors and not text:
        text = "STOPPED: " + " | ".join(e for e in errors if e)
    return text, usage


def record_cost(task_id: str, phase: str, usage: dict, log) -> None:
    """One line per session in ~/.assay/helper-costs.jsonl, and one on the terminal."""
    if not usage:
        return
    COSTS.parent.mkdir(parents=True, exist_ok=True)
    with COSTS.open("a") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "task": task_id, "phase": phase, **usage}) + "\n")
    log(f"  {phase}: {usage['total_tokens']:,} tokens, ${usage['cost_usd']:.3f}, "
        f"{usage['duration_ms'] / 1000:.0f}s, {usage['turns']} turns")


def keep_transcript(task_id: str, attempt: int, report: str) -> None:
    """The whole report, on disk, because the log line is the last sentence and
    the reason a session stopped is usually three lines above it."""
    folder = WORK.parent / "helper-logs"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{task_id}-{attempt}.txt").write_text(report)


def decide_prompt(task, spec: str, public_suite: str) -> str:
    return (PROMPT.read_text() + "\n\n"
            "THIS PASS: decide only. Do not bid, fetch or submit — the loop places the bid.\n"
            f"Task {task['id']}: {task.get('title', '')}\n"
            f"Price Assay will bid: {price_for(task)} credits; ETA {eta_for(task)} seconds.\n\n"
            + as_untrusted("THE JOB", spec) + "\n\n"
            + as_untrusted("THE PUBLIC TESTS", public_suite) + "\n\n"
            "Answer with exactly one line: YES if you are confident you can implement this "
            "in Python from what is here, otherwise NO and why.")


def work_prompt(award) -> str:
    task_id = award["taskId"]
    work = WORK_COMMAND
    return (PROMPT.read_text() + "\n\n"
            f"THIS PASS: you hold award {award['awardId']} on task {task_id} "
            f"(lease runs to {award.get('leaseExpiresAt', '?')}). Use the skill's plumbing, not "
            "the marketplace's API directly:\n"
            f"  1. `{work} fetch {task_id}` — lays the package out under {WORK}/{task_id}/package "
            "and prints the job.\n"
            "  2. Do the work in that directory and nowhere else, touching only the paths it says you may.\n"
            f"  3. `{work} check {task_id}` — the public tests, in a container. If this command "
            "cannot run at all (no docker, no image), STOP and say so; do not submit.\n"
            f"  4. When they pass: `{work} submit {task_id}`.\n"
            "End with one line: SUBMITTED, or STOPPED and why.")


# ── one pass ────────────────────────────────────────────────────────────────

def one_pass(now=None, log=print) -> dict:
    now = now or datetime.now(timezone.utc)
    state = load_state()
    did = {"bid": [], "submitted": [], "skipped": []}

    # Awards first: a lease is running down.
    state.setdefault("attempts", {})
    for award in awards_needing_work(call("GET", "/v1/awards/mine"), set(state["submitted"]), state["attempts"]):
        log(f"award held on {award['taskId']} ({award.get('title', '')}) — starting the session")
        state["attempts"][award["taskId"]] = state["attempts"].get(award["taskId"], 0) + 1
        save_state(state)
        report = ask_claude(work_prompt(award), timeout=3600)
        record_cost(award["taskId"], "work", SESSION["usage"], log)
        keep_transcript(award["taskId"], state["attempts"][award["taskId"]], report)
        log(report.strip().splitlines()[-1] if report.strip() else "(no report)")
        if "SUBMITTED" in report:
            state["submitted"].append(award["taskId"])
            did["submitted"].append(award["taskId"])
        save_state(state)

    # Then the board.
    missing = toolchain_ready()
    for task in call("GET", "/v1/tasks") or []:
        if not worth_asking(task, now, set(state["bid"])):
            continue
        detail = call("GET", f"/v1/tasks/{task['id']}")
        suite = (detail or {}).get("publicSuite") or ""
        if not looks_like_python(suite):
            did["skipped"].append((task["id"], "not a Python package"))
            continue
        if missing:
            did["skipped"].append((task["id"], "this machine lacks " + ", ".join(missing)))
            continue
        answer = ask_claude(decide_prompt(task, (detail or {}).get("specMarkdown", ""), suite), timeout=600)
        record_cost(task["id"], "decide", SESSION["usage"], log)
        if answer.strip().upper().startswith("YES") or "\nYES" in answer.upper():
            price, eta = price_for(task), eta_for(task)
            call("POST", f"/v1/tasks/{task['id']}/bids",
                 {"price": price, "etaSeconds": eta, "modelDeclared": model_declared()})
            log(f"bid {price} on {task['id']} ({task.get('title', '')})")
            state["bid"].append(task["id"])
            did["bid"].append(task["id"])
        else:
            did["skipped"].append((task["id"], "the session said no"))
            state["bid"].append(task["id"])   # asked once; do not ask every minute
        save_state(state)

    for task_id, why in did["skipped"]:
        log(f"skipped {task_id}: {why}")
    return did


PIDFILE = WORK.parent / "helper.pid"


def already_running() -> int:
    """The pid of another helper on this machine, or 0. Two loops on one key
    would both work the same award — the owner restarted without stopping
    the first and had two within a minute of each other."""
    try:
        pid = int(PIDFILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0
    if pid == os.getpid():
        return 0
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return 0
    except PermissionError:
        return pid
    return pid


def main(argv) -> int:
    try:
        load_key()
    except AssayError as problem:
        print(problem, file=sys.stderr)
        return 2
    other = already_running()
    if other:
        print(f"a helper is already running on this machine (pid {other}). Stop it first, or leave it; "
              "two would work the same job twice.", file=sys.stderr)
        return 3
    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(os.getpid()))
    once = len(argv) > 1 and argv[1] == "once"
    # Say who this is and that it is awake. The first person to run it saw a
    # blank terminal for a minute and reported "this is not doing nothing" —
    # silence read as failure, because nothing said it was the intended state.
    try:
        me = call("GET", "/v1/agents/me") or {}
        print(f"helper {me.get('handle', '?')} · {me.get('tier', '?').lower().replace('_', ' ')} · "
              f"looking for work every {INTERVAL}s · leave this running", flush=True)
    except AssayError as problem:
        print(problem, file=sys.stderr)
        return 2
    missing = toolchain_ready()
    if missing:
        print("this machine lacks: " + ", ".join(missing), file=sys.stderr, flush=True)
    while True:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        try:
            did = one_pass(log=lambda line: print(f"[{stamp}] {line}", flush=True))
            if not any(did.values()):
                print(f"[{stamp}] nothing to do — no job I can go for, no award held", flush=True)
        except AssayError as problem:
            print(f"[{stamp}] {problem}", file=sys.stderr, flush=True)
        except subprocess.TimeoutExpired:
            print(f"[{stamp}] the session ran out of time", file=sys.stderr, flush=True)
        if once:
            return 0
        time.sleep(INTERVAL)


if __name__ == "__main__":
    # Frozen: `assay-helper work <verb> …` is work.py, so the session the
    # loop starts has the plumbing without a python on the machine.
    if FROZEN and len(sys.argv) > 1 and sys.argv[1] == "work":
        import work  # noqa: E402  (bundled beside this file)
        sys.exit(work.main(sys.argv[1:]))
    sys.exit(main(sys.argv))
