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
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from assay import AssayError, as_untrusted, call, ensure_image, load_key, sandbox_image  # noqa: E402

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


def tool_name() -> str:
    """The tool's name on the marketplace (E26): claude-code or codex."""
    return "codex" if session_runner() == "codex" else "claude-code"


# The model the last session said it ran, from its own stream (E26): the
# honest reading, once there has been one.
SEEN_MODEL: dict = {"model": None}


def configured_model() -> str:
    """The model the tool is set to, where it keeps it: Codex's config.toml
    `model`, Claude Code's settings.json `model`; the vendor's default word
    when neither says. Declared, never verified."""
    if SEEN_MODEL.get("model"):
        return SEEN_MODEL["model"]
    try:
        if session_runner() == "codex":
            text = (pathlib.Path.home() / ".codex" / "config.toml").read_text()
            m = re.search(r'(?m)^\s*model\s*=\s*"([^"]+)"', text)
            return m.group(1) if m else "default"
        settings = json.loads((pathlib.Path.home() / ".claude" / "settings.json").read_text())
        model = settings.get("model")
        return str(model) if model else "default"
    except Exception:  # noqa: BLE001 — a missing file is "default"
        return "default"


def model_declared() -> str:
    """What the bid says it runs on: tool/model (E26). A claim nobody verifies,
    so it is at least honest about the product and what it is set to."""
    return f"{tool_name()}/{configured_model()}"


def models_offered() -> list:
    """Every model the tool offers, verbatim (E26 Decision 2): Codex keeps the
    list its account is allowed at ~/.codex/models_cache.json; Claude Code's
    are its families. Nothing curated; the marketplace shows the counts."""
    if session_runner() == "codex":
        try:
            d = json.loads((pathlib.Path.home() / ".codex" / "models_cache.json").read_text())
            models = d.get("models") if isinstance(d, dict) else d
            names = [(m.get("slug") or m.get("id")) if isinstance(m, dict) else str(m) for m in (models or [])]
            return [n for n in names if n]
        except Exception:  # noqa: BLE001
            return []
    return ["claude-opus-5", "claude-sonnet-5", "claude-fable-5-1", "claude-haiku-4-5-20251001"]


def report_models(log=print) -> None:
    """Tell the marketplace what this helper can run, once a start (E26)."""
    models = models_offered()
    configured = configured_model()
    if configured != "default" and configured not in models:
        models = [configured] + models
    try:
        call("POST", "/v1/agents/me/models", {"tool": tool_name(), "models": models})
        log(f"runs {tool_name()} as {configured}; {len(models)} models on offer")
    except AssayError as problem:
        log(f"could not report the models: {problem}")


def meant_for_me(task) -> str:
    """Why this task is not for this helper (E26), or "" when it may bid: the
    task names who may, as prefixes of tool/model, and this helper's
    declaration is not among them. Read before the session is asked, so a
    refusal at the bid never costs a decision."""
    allowed = task.get("allowedModels") or []
    if not allowed:
        return ""
    mine = model_declared()
    for a in allowed:
        if mine == a or mine.startswith(a + "/") or mine.startswith(a + "-") or mine.startswith(a + "."):
            return ""
    return f"it asks for {', '.join(allowed)}; this helper runs {mine}"


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


def can_take(task, public_suite: str, log=print) -> str:
    """Why this helper cannot take the task, in words, or "" when it can (E22).

    The one check is the box: a task whose tests run names the sandbox they
    run in (E22 Part 2), and the helper builds that box the first time it is
    needed. Nothing here reads the tests to guess a language. There was such
    a rule — "its tests do not look like Python", wanting `def test_` or
    `import pytest` — and it skipped the owner's second real task, a plain
    Python script that `run.sh` runs and that writes its own report. The
    task says where its tests run and `run.sh` says how; whether they pass
    is `work check`'s to find out, in the box, the way the verifier will. A
    task where nothing runs — no runner, the buyer judges — is any kind of
    work. A server older than E22 says nothing about `runnable`; that server
    required a runner on every task, so nothing said means it runs.
    """
    if not task.get("runnable", True):
        return ""
    sandbox = str(task.get("sandbox") or "python")
    if not ensure_image(sandbox, log):
        return f"this machine lacks the {sandbox_image(sandbox)} image and could not build it"
    return ""


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
    if shutil.which("git") is None:
        # E23: a submission with a file that is not text is a git binary patch,
        # which `git diff` writes; the text case needs no git.
        missing.append("git (a deck or an image comes back as a git patch)")
    if shutil.which("docker") is None:
        missing.append("docker (work.py check runs the tests in a container)")
    # The sandbox image is asked about per task now (can_take, E22 Part 2),
    # and built if the machine lacks it — once, from the Dockerfile bundled
    # beside these scripts.
    if shutil.which(CLAUDE) is None and shutil.which(CODEX) is None:
        missing.append(f"{CLAUDE} or {CODEX} (the coding session that does the work)")
    return missing




def out_of_reach(task, standing) -> str:
    """Why the marketplace would refuse this helper's bid before the session
    is asked, in words, or "" when it would not.

    The gate (E4, `BidGate`) caps an unproven agent at a task value and at a
    number of open bids, and `/v1/agents/me/standing` says both. Read here
    first because asking the session costs money: on 2026-09-16 the owner's
    desktop app said YES to a 12,000 task every minute, was refused every
    minute as an unproven agent capped at 10,000, and paid for the decision
    each time. A refusal the helper can see coming is a line in the log and
    no question asked.
    """
    if not isinstance(standing, dict):
        return ""
    ceiling = standing.get("taskValueCeiling")
    if ceiling is not None and int(task.get("maxBudget") or 0) > int(ceiling):
        return (f"its budget is {task.get('maxBudget')} and an unproven helper may take up to "
                f"{ceiling}; smaller tasks lift the ceiling")
    cap = standing.get("openBidCap")
    if cap is not None and int(standing.get("openBids") or 0) >= int(cap):
        return f"I already hold {standing.get('openBids')} open bids, the cap for now"
    return ""


def worth_asking(task, now: datetime, already_bid: set) -> bool:
    """Open, still accepting bids, not already bid on by this helper."""
    return (task.get("status") == "OPEN"
            and task.get("id") not in already_bid
            and bidding_open(task, now))


def work_key(award) -> str:
    """What the state remembers a piece of work by. The award, since E27: a
    revision is a second award on the same task, and remembering the task
    would have hidden it. Older state holds task ids; both are honoured."""
    return award.get("awardId") or award.get("taskId")


def awards_needing_work(awards, submitted: set, attempts: dict = None) -> list:
    """Held, still active, not yet submitted, and not already tried ATTEMPTS_PER_AWARD times."""
    attempts = attempts or {}
    return [a for a in awards or []
            if a.get("status") == "ACTIVE"
            and work_key(a) not in submitted
            and (a.get("revisionReason") or a.get("taskId") not in submitted)
            and attempts.get(work_key(a), 0) < ATTEMPTS_PER_AWARD]


def rejections_to_answer(awards, replied: set) -> list:
    """E27 Rule 3: awards the buyer rejected with a reason, whose reply window is
    open and which this helper has not answered."""
    return [a for a in awards or []
            if a.get("rejectionReason") and a.get("awardId") not in replied]


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
    SESSION["steps"] = []
    if session_runner() == "codex":
        # Codex's headless mode. The same trust as the Claude line below —
        # dontAsk with Bash is a session that runs what it decides, and so is
        # this flag; the guard is E9 Decision 3's scratch root and work.py's
        # container, not the session's own sandbox, which would also block
        # the docker socket `work check` needs. --ignore-user-config: no MCP
        # servers, no project rules — the session gets the prompt and the
        # tools, as Claude does with an empty --mcp-config.
        stdout, stderr, code = run_streaming(
            [CODEX, "exec", "--json", "--ignore-user-config", "--skip-git-repo-check",
             "--ephemeral", "--dangerously-bypass-approvals-and-sandbox", "-C", str(WORK), prompt],
            timeout, codex_step)
        text, usage = parse_codex_session(stdout)
    else:
        # stream-json (E23): one event per line as the session works, so the
        # steps can be shown while they happen; the last line is the same
        # result the json format returned, and is read the same way.
        stdout, stderr, code = run_streaming(
            [CLAUDE, "-p", prompt, "--output-format", "stream-json", "--verbose",
             "--permission-mode", "dontAsk",
             "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep,Task",
             "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"],
            timeout, claude_step)
        text, usage = parse_session(last_json_line(stdout))
    if usage and not usage.get("duration_ms"):
        usage["duration_ms"] = int((time.monotonic() - started) * 1000)
    SESSION["usage"] = usage
    return text + (("\n" + stderr) if code != 0 else "")


def run_streaming(command, timeout: int, step_of) -> tuple:
    """Runs the session and reads its stdout line by line as it comes (E23
    Decision 6). Each line is offered to `step_of`, which turns the event into
    a plain line for a person or None; the line goes to the log at once and
    onto SESSION["steps"] for the marketplace. Returns (stdout, stderr, code)."""
    process = subprocess.Popen(command, cwd=WORK, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1, stdin=subprocess.DEVNULL)
    lines = []
    deadline = time.monotonic() + timeout
    try:
        # readline rather than iterating the pipe, so a line is handed on the
        # moment it ends; the session flushes one event a line.
        for line in iter(process.stdout.readline, ""):
            lines.append(line)
            step = step_of(line)
            if step:
                SESSION["steps"].append(step)
                SESSION["log"]("  " + "  " * step[0] + step[1])
                # E25: the buyer follows the work as it happens, so each step
                # goes to the marketplace now, not in a batch at the end.
                # Best effort, off this thread: a slow post must not hold up
                # the reading of the next line.
                if SESSION.get("award"):
                    threading.Thread(target=post_steps_so_far, args=(SESSION["award"],), daemon=True).start()
            if time.monotonic() > deadline:
                process.kill()
                break
        stderr = process.stderr.read()
        code = process.wait(timeout=30)
    except Exception:
        process.kill()
        raise
    return "".join(lines), stderr or "", code


def last_json_line(stdout: str) -> str:
    """The result event of a stream-json session, as the json format would have printed it."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if d.get("type") == "result":
            return line
    return stdout


# ── the steps a session takes, in a person's words (E23 Decision 6) ─────────

def short_path(path) -> str:
    """A path — or a command naming paths — as the person reads it: relative
    to the scratch root, never the machine's. One line, 120 characters."""
    text = str(path or "").replace(str(WORK) + "/", "").replace(str(WORK), ".")
    text = " ".join(text.split())
    return text[:120]


def claude_step(line: str):
    """One stream-json event from Claude Code into (depth, line), or None.

    A tool use is a step; a subagent's tool use carries parent_tool_use_id and
    sits one level in. Nothing from a file's contents is kept — the action and
    the path only."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if d.get("type") == "assistant" and (d.get("message") or {}).get("model"):
        SEEN_MODEL["model"] = str(d["message"]["model"])
    if d.get("type") != "assistant":
        return None
    depth = 1 if d.get("parent_tool_use_id") else 0
    for block in (d.get("message") or {}).get("content") or []:
        if block.get("type") != "tool_use":
            continue
        name, inp = block.get("name"), block.get("input") or {}
        if name == "Read":     return (depth, "reading " + short_path(inp.get("file_path")))
        if name == "Write":    return (depth, "writing " + short_path(inp.get("file_path")))
        if name == "Edit":     return (depth, "editing " + short_path(inp.get("file_path")))
        if name == "Bash":     return (depth, "running: " + short_path(inp.get("command")))
        if name in ("Glob", "Grep"): return (depth, "searching for " + str(inp.get("pattern") or "")[:80])
        if name == "Task":     return (depth, "asked a subagent: " + short_path(inp.get("description")))
        # The session's own machinery — loading a skill, finding a tool — is
        # not a step of the work and is not shown to the buyer (2026-09-19).
        if name in ("Skill", "ToolSearch", "TodoWrite", "AskUserQuestion"):
            return None
        return (depth, "using " + str(name))
    return None


def codex_step(line: str):
    """One JSONL event from `codex exec --json` into (depth, line), or None."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if d.get("type") != "item.started" and d.get("type") != "item.completed":
        return None
    item = d.get("item") or {}
    kind = item.get("type")
    if d.get("type") == "item.started" and kind == "command_execution":
        return (0, "running: " + str(item.get("command") or "")[:120])
    if d.get("type") == "item.completed" and kind == "file_change":
        verbs = {"add": "writing", "update": "editing", "delete": "deleting"}
        changes = item.get("changes") or []
        if changes:
            c = changes[0]
            return (0, verbs.get(str(c.get("kind")), "changing") + " " + short_path(c.get("path")))
    if d.get("type") == "item.started" and kind == "web_search":
        return (0, "searching the web")
    return None


def post_steps(award_id: str, steps: list, log) -> None:
    """The session's steps, to the marketplace, beside the award (E23 Decision 6).
    Best effort: a failure here is logged and does not fail the job. Since E25
    the steps go up as they happen (post_steps_so_far); this sends whatever is
    still unsent at the end, so nothing is lost if a live post failed."""
    unsent = steps[SESSION.get("posted", 0):2000]
    if not unsent:
        return
    try:
        call("POST", f"/v1/awards/{award_id}/steps",
             {"steps": [{"depth": d, "line": l} for d, l in unsent]})
        SESSION["posted"] = SESSION.get("posted", 0) + len(unsent)
    except Exception as problem:  # noqa: BLE001 — the work is done; the log is extra
        log(f"could not send the steps: {problem}")


POST_LOCK = threading.Lock()


def post_steps_so_far(award_id: str) -> None:
    """Every step not yet sent, sent now (E25). One at a time: two threads
    posting the same steps would write them twice, and the server appends."""
    with POST_LOCK:
        steps = SESSION.get("steps") or []
        unsent = steps[SESSION.get("posted", 0):2000]
        if not unsent:
            return
        try:
            call("POST", f"/v1/awards/{award_id}/steps",
                 {"steps": [{"depth": d, "line": l} for d, l in unsent]}, timeout=10)
            SESSION["posted"] = SESSION.get("posted", 0) + len(unsent)
        except Exception:  # noqa: BLE001 — the end-of-job post sends what this could not
            pass


# The last session's bill, for the caller to record. A module dict rather
# than an attribute on the function, so a test can stand a lambda in for
# ask_claude and still say what it cost.
SESSION: dict = {"usage": {}, "steps": [], "log": print}


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
    the reason a session stopped is usually three lines above it. Named by the
    award since E27: a revision is a second award on the same task and its
    transcript overwrote the first's when they shared a name (2026-09-19)."""
    folder = WORK.parent / "helper-logs"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{task_id}-{attempt}.txt").write_text(report)


def decide_prompt(task, spec: str, public_suite: str, runnable: bool = True) -> str:
    # E22: with nothing to run, the job is whatever the spec asks for and the
    # buyer decides by looking at it — so the question is whether it can be
    # done. With tests, the question is whether these tests can be made to
    # pass; the language is whatever the package and its run.sh use.
    judged = (as_untrusted("THE PUBLIC TESTS", public_suite) + "\n\n"
              "The package's run.sh runs these in the "
              + ("Node" if str(task.get("sandbox") or "python") == "node" else "Python")
              + " box, and they decide. "
              "Answer with exactly one line: YES if you are confident you can do this and "
              "make these tests pass from what is here, otherwise NO and why."
              if runnable else
              "Nothing runs on this one: there are no tests, and the buyer looks at the work "
              "and says yes or no. The work can be any kind — text, a document, a deck, a "
              "page — as files in the package's folder.\n\n"
              "Answer with exactly one line: YES if you are confident you can do this well "
              "from what is here, otherwise NO and why.")
    return (PROMPT.read_text() + "\n\n"
            "THIS PASS: decide only. Do not bid, fetch or submit — the loop places the bid.\n"
            f"Task {task['id']}: {task.get('title', '')}\n"
            f"Price Assay will bid: {price_for(task)} credits; ETA {eta_for(task)} seconds.\n\n"
            + as_untrusted("THE JOB", spec) + "\n\n"
            + judged)


def reply_prompt(award) -> str:
    """The helper's one reply to a rejection (E27 Rule 3): plain, for the judge."""
    return (PROMPT.read_text() + "\n\n"
            f"THIS PASS: reply only; do no work and change no files. The buyer rejected the work you "
            f"submitted on task {award['taskId']} ({award.get('title', '')}) with this reason:\n\n"
            + as_untrusted("THE BUYER'S REASON", award["rejectionReason"]) + "\n\n"
            f"Your earlier work is under {WORK}/{award['taskId']}/package if it is still here; read it if "
            "you need to. Write a reply of one paragraph at most, addressed to a judge who will read the "
            "spec, your work and this reason: say plainly what the work does that meets the spec, or "
            "what the reason misses, or, if the reason is right, say so. No promises, no offers to "
            "redo it. Output the reply and nothing else.")


def work_prompt(award) -> str:
    task_id = award["taskId"]
    work = WORK_COMMAND
    if award.get("revisionReason"):
        return (PROMPT.read_text() + "\n\n"
                f"THIS PASS: a revision. The buyer looked at the work you submitted on task {task_id} "
                f"and asked for one change, with this reason:\n\n"
                + as_untrusted("THE BUYER'S REASON", award["revisionReason"]) + "\n\n"
                f"You hold award {award['awardId']} (lease runs to {award.get('leaseExpiresAt', '?')}). "
                f"Your earlier work is under {WORK}/{task_id}/package; do NOT run `{work} fetch` on it "
                "(that lays the original package down again over your changes). If that directory is "
                f"gone, fetch and do the whole job again. Revise the work there to answer the reason, "
                f"then `{work} check {task_id}` and, when it passes, `{work} submit {task_id}`.\n"
                "End with one line: SUBMITTED, or STOPPED and why.")
    return (PROMPT.read_text() + "\n\n"
            f"THIS PASS: you hold award {award['awardId']} on task {task_id} "
            f"(lease runs to {award.get('leaseExpiresAt', '?')}). Use the skill's plumbing, not "
            "the marketplace's API directly:\n"
            f"  1. `{work} fetch {task_id}` — lays the package out under {WORK}/{task_id}/package "
            "and prints the job.\n"
            "  2. Do the work in that directory and nowhere else, touching only the paths it says you may.\n"
            f"  3. `{work} check {task_id}` — the public tests, in a container. If this command "
            "cannot run at all (no docker, no image), STOP and say so; do not submit. If it says "
            "there is nothing to run, the buyer judges by looking: read your work over as they "
            "would, then go on.\n"
            f"  4. When they pass: `{work} submit {task_id}`.\n"
            "End with one line: SUBMITTED, or STOPPED and why.")


# ── one pass ────────────────────────────────────────────────────────────────

def one_pass(now=None, log=print) -> dict:
    now = now or datetime.now(timezone.utc)
    state = load_state()
    did = {"bid": [], "submitted": [], "skipped": []}

    # Awards first: a lease is running down.
    state.setdefault("attempts", {})
    mine = call("GET", "/v1/awards/mine")
    # E27: a rejection with a reason gets one reply from a short session, so the
    # judge hears both sides. Before the work: the window is ten minutes.
    state.setdefault("replied", [])
    for award in rejections_to_answer(mine, set(state["replied"])):
        log(f"the buyer rejected {award['taskId']}: {award['rejectionReason']} — writing the reply")
        state["replied"].append(award["awardId"])
        save_state(state)
        answer = ask_claude(reply_prompt(award), timeout=300)
        record_cost(award["taskId"], "reply", SESSION["usage"], log)
        text = (answer or "").strip()
        if text:
            try:
                call("POST", f"/v1/awards/{award['awardId']}/reply", {"reply": text[:4000]})
                log(f"replied on {award['taskId']}")
            except AssayError as problem:
                log(f"could not send the reply: {problem}")
    for award in awards_needing_work(mine, set(state["submitted"]), state["attempts"]):
        revising = bool(award.get("revisionReason"))
        log(f"award held on {award['taskId']} ({award.get('title', '')}) — "
            + ("a revision: " + award["revisionReason"] if revising else "starting the session"))
        state["attempts"][work_key(award)] = state["attempts"].get(work_key(award), 0) + 1
        save_state(state)
        SESSION["log"] = log
        SESSION["award"] = award["awardId"]
        SESSION["posted"] = 0
        report = ask_claude(work_prompt(award), timeout=3600)
        SESSION["award"] = None
        record_cost(award["taskId"], "work", SESSION["usage"], log)
        post_steps(award["awardId"], SESSION.get("steps") or [], log)
        keep_transcript(work_key(award), state["attempts"][work_key(award)], report)
        log(report.strip().splitlines()[-1] if report.strip() else "(no report)")
        if "SUBMITTED" in report:
            state["submitted"].append(work_key(award))
            did["submitted"].append(award["taskId"])
        save_state(state)

    # Then the board.
    missing = toolchain_ready()
    standing = call("GET", "/v1/agents/me/standing")
    for task in call("GET", "/v1/tasks") or []:
        if not worth_asking(task, now, set(state["bid"])):
            continue
        if task.get("mine"):
            # The buyer's own key in the helper (2026-09-17): the marketplace
            # refuses a bid on your own task, and the session should not be
            # asked about it at all. The feed says "mine" only to the caller.
            did["skipped"].append((task["id"], "it is your own task; a helper needs its own agent"))
            continue
        not_mine = meant_for_me(task)
        if not_mine:
            did["skipped"].append((task["id"], not_mine))
            continue
        beyond = out_of_reach(task, standing)
        if beyond:
            # Not remembered: standing changes, and a task above the ceiling
            # today may be within it after the next small one is paid.
            did["skipped"].append((task["id"], beyond))
            continue
        detail = call("GET", f"/v1/tasks/{task['id']}")
        suite = (detail or {}).get("publicSuite") or ""
        why_not = can_take(task, suite, log)
        if why_not:
            did["skipped"].append((task["id"], why_not))
            continue
        if missing:
            did["skipped"].append((task["id"], "this machine lacks " + ", ".join(missing)))
            continue
        answer = ask_claude(decide_prompt(task, (detail or {}).get("specMarkdown", ""), suite,
                                          runnable=task.get("runnable", True)), timeout=600)
        record_cost(task["id"], "decide", SESSION["usage"], log)
        if answer.strip().upper().startswith("YES") or "\nYES" in answer.upper():
            price, eta = price_for(task), eta_for(task)
            try:
                call("POST", f"/v1/tasks/{task['id']}/bids",
                     {"price": price, "etaSeconds": eta, "modelDeclared": model_declared()})
            except AssayError as refused:
                # The marketplace said no in words; they go in the log, and the
                # task is remembered so the session is not asked again next
                # minute. Before this, the error left the pass and the state
                # unsaved, so the same YES was bought once a minute. One
                # refusal is not remembered: no credits for the bond (402),
                # which the person fixes by funding the helper, after which
                # the task should be tried again without a restart.
                did["skipped"].append((task["id"], f"the marketplace refused the bid: {refused}"))
                if not str(refused).startswith("402."):
                    state["bid"].append(task["id"])
                    save_state(state)
                continue
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
        print(f"helper {me.get('handle', '?')}, {me.get('tier', '?').lower().replace('_', ' ')}, "
              f"looking for work every {INTERVAL}s. Leave this running.", flush=True)
    except AssayError as problem:
        print(problem, file=sys.stderr)
        return 2
    missing = toolchain_ready()
    if missing:
        print("this machine lacks: " + ", ".join(missing), file=sys.stderr, flush=True)
    report_models(lambda line: print(line, flush=True))
    while True:
        # The stamp is taken per line, not per pass: with one stamp a pass
        # long, every step of a two-minute session printed with the same time
        # (the E23 walk), which made a live log look like a dump.
        stamp = lambda: datetime.now(timezone.utc).strftime("%H:%M:%S")  # noqa: E731
        try:
            did = one_pass(log=lambda line: print(f"[{stamp()}] {line}", flush=True))
            if not any(did.values()):
                print(f"[{stamp()}] nothing to do — no job I can go for, no award held", flush=True)
        except AssayError as problem:
            print(f"[{stamp()}] {problem}", file=sys.stderr, flush=True)
        except subprocess.TimeoutExpired:
            print(f"[{stamp()}] the session ran out of time", file=sys.stderr, flush=True)
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
