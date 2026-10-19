#!/usr/bin/env python3
"""Post-commit docstring freshness check.

Design and rationale: requirements.md in this directory.
"""
import difflib
import functools
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

TAG = "[docstring-check]"
CLAUDE_TIMEOUT_SECONDS = 300
SBX_TIMEOUT_SECONDS = 120
SBX_CREATE_TIMEOUT_SECONDS = 900

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["up_to_date", "stale"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "qualified_name": {"type": "string"},
                    "docstring_start_line": {"type": "integer"},
                    "current_docstring": {"type": "string"},
                    "proposed_docstring": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": [
                    "file",
                    "qualified_name",
                    "docstring_start_line",
                    "current_docstring",
                    "proposed_docstring",
                    "reason",
                ],
            },
        },
    },
    "required": ["status", "summary", "findings"],
}

REVISION_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "proposed_docstring": {"type": "string"},
    },
    "required": ["message", "proposed_docstring"],
}

DETECT_PROMPT = """You are checking whether Python docstrings are still accurate after a commit.

Commit: {sha}
Changed Python files under api/:
{files}

Inspect the change with `git show {sha}`. Docstrings often sit outside the diff's context lines, so read the full file as of this commit with `git show {sha}:<path>` where needed.

For each function, method, class, or module whose code this commit changed, decide whether its existing docstring still accurately describes the code as of this commit. Only flag a docstring when the code change made it inaccurate or incomplete: removed, renamed, or added parameters; a changed return value; changed behavior; changed exceptions. Do not flag wording or style preferences, and do not flag missing docstrings.

For each stale docstring:
- file: repo-relative path.
- qualified_name: dotted name, e.g. Project.get_download_config.
- docstring_start_line: 1-indexed line of the opening quotes in the file as of this commit.
- current_docstring: copied character for character from `git show {sha}:<path>`, starting at the opening quotes and ending at the closing quotes, with every continuation line's indentation intact. It must occur exactly once in that file.
- proposed_docstring: the full replacement in the same form (same quote style, same continuation-line indentation), changing only what the code change requires.
- reason: one sentence on what changed in the code that makes the current docstring inaccurate.

Set status to "stale" if there is at least one finding, otherwise "up_to_date" with an empty findings list. Keep summary to one sentence."""

REVISION_PROMPT = """The developer gave feedback on your proposed docstring for {qualified_name} in {file}.

Your latest proposal:
{proposal}

Feedback:
{feedback}

Revise the proposal. proposed_docstring replaces the same current_docstring as before, so return the full replacement starting at the opening quotes and ending at the closing quotes, with the same quote style and the file's indentation on every continuation line. Keep message to one short sentence describing what you changed."""


COMMANDS = {"y": "y", "yes": "y", "n": "n", "no": "n", "q": "q", "quit": "q"}


class ClaudeUnavailable(Exception):
    pass


def git_out(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout.strip()


def skip_reason():
    """Return why the hook should stay silent for this commit, or None to run."""
    for marker in ("rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        if Path(git_out("rev-parse", "--git-path", marker)).exists():
            return "history operation in progress"
    parents = git_out("rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    if len(parents) != 1:
        return "root or merge commit"
    return None


def changed_python_files():
    out = git_out("diff", "--name-only", "--diff-filter=AMR", "HEAD~1", "HEAD", "--", "api/*.py")
    return [line for line in out.splitlines() if line]


def open_tty():
    """Git runs hooks with stdin at /dev/null, so prompts read the terminal directly."""
    try:
        return open("/dev/tty", "r"), open("/dev/tty", "w")
    except OSError:
        return None


def ask(tty, prompt):
    reader, writer = tty
    writer.write(prompt)
    writer.flush()
    line = reader.readline()
    if not line:
        raise EOFError
    return line.strip()


@functools.lru_cache(maxsize=None)
def repo_root():
    return git_out("rev-parse", "--show-toplevel")


def sandbox_name():
    slug = re.sub(r"[^A-Za-z0-9.-]", "-", Path(repo_root()).name)
    return f"docstring-check-{slug}"


def describe_failure(result):
    lines = [line.strip() for line in (result.stderr or result.stdout).splitlines() if line.strip()]
    if not lines:
        return f"exit code {result.returncode}"
    return " / ".join(lines[-5:])


def run_sbx(*args, timeout):
    try:
        return subprocess.run(["sbx", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ClaudeUnavailable(f"`sbx {args[0]}` timed out after {timeout}s")


def ensure_sandbox():
    """Create the dedicated docstring sandbox, scoped to this repo, on first use."""
    name = sandbox_name()
    listed = run_sbx("ls", "-q", timeout=SBX_TIMEOUT_SECONDS)
    if listed.returncode != 0:
        raise ClaudeUnavailable(f"`sbx ls` failed ({describe_failure(listed)})")
    # Don't re-create sandbox if it already exists
    if name in listed.stdout.split():
        return
    print(f"{TAG} Creating the dedicated sandbox {name} (first run only; this may take a moment)...")
    created = run_sbx(
        "create", "--name", name, "--no-progress", "claude", repo_root(),
        timeout=SBX_CREATE_TIMEOUT_SECONDS,
    )
    if created.returncode != 0:
        raise ClaudeUnavailable(f"couldn't create sandbox {name} ({describe_failure(created)})")


def run_claude(prompt, tools, schema, resume=None, allowed_tools=None):
    """Run one headless, read-only Claude call inside the dedicated sandbox.

    Returns (structured_output, session_id).
    """
    cmd = [
        "exec",
        "-w",
        repo_root(),
        "-e",
        "CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1",
        sandbox_name(),
        "claude",
        "-p",
        prompt,
        "--model",
        "sonnet",
        "--restricted",
        "--tools",
        tools,
        "--permission-prompts",
        "none",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema),
    ]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    if resume:
        cmd += ["--resume", resume]
    result = run_sbx(*cmd, timeout=CLAUDE_TIMEOUT_SECONDS)
    payload = None
    for line in reversed(result.stdout.splitlines()):
        try:
            payload = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(payload, dict):
        raise ClaudeUnavailable(f"Claude Code call failed ({describe_failure(result)})")
    output = payload.get("structured_output")
    if payload.get("is_error") or not output:
        raise ClaudeUnavailable(
            f"Claude Code returned an error ({payload.get('result') or payload.get('subtype')})"
        )
    return output, payload.get("session_id")


def read_working(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def apply_replacement(finding, proposal):
    """Replace the docstring in the working-tree file; nothing is staged or committed."""
    path, current = finding["file"], finding["current_docstring"]
    text = read_working(path)
    if text.count(current) != 1:
        return False
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text.replace(current, proposal, 1))
    return True


def list_findings(findings):
    for index, finding in enumerate(findings, 1):
        print(
            f"  {index}. {finding['file']}:{finding['docstring_start_line']}  "
            f"{finding['qualified_name']}"
        )
        print(f"     {finding['reason']}")


def show_proposal(index, total, finding, proposal):
    print(f"\n{TAG} ({index}/{total}) {finding['file']} :: {finding['qualified_name']}")
    diff = difflib.unified_diff(
        finding["current_docstring"].splitlines(),
        proposal.splitlines(),
        lineterm="",
        n=1000,
    )
    for line in list(diff)[3:]:
        print(f"  {line}")


def review(findings, session_id, tty, applied, tally):
    """Walk each proposal; like `git add -p`, a "y" takes effect immediately.

    Accepted replacements are written to the working tree as they're accepted and
    recorded in `applied`, so they stay even if the developer quits later.
    Returns True if the developer quit early.
    """
    for index, finding in enumerate(findings, 1):
        if read_working(finding["file"]).count(finding["current_docstring"]) != 1:
            print(
                f"\n{TAG} Couldn't locate the current docstring for "
                f"{finding['qualified_name']} exactly once in {finding['file']}; skipping it."
            )
            tally["not located"] += 1
            continue
        proposal = finding["proposed_docstring"]
        while True:
            show_proposal(index, len(findings), finding, proposal)
            answer = ask(
                tty,
                "Apply this replacement? (y)es, (n)o, (q)uit, or type feedback here "
                "for Claude to improve it: ",
            )
            key = COMMANDS.get(answer.lower())
            if key == "y":
                if apply_replacement(finding, proposal):
                    applied.append(finding)
                    tally["accepted"] += 1
                else:
                    print(
                        f"{TAG} {finding['qualified_name']} in {finding['file']} changed "
                        "during review (edited by hand?); leaving it alone."
                    )
                    tally["not located"] += 1
                break
            if key == "n":
                tally["rejected"] += 1
                break
            if key == "q":
                return True
            if not answer:
                continue
            print(f"{TAG} Revising...")
            try:
                revision, _ = run_claude(
                    REVISION_PROMPT.format(
                        qualified_name=finding["qualified_name"],
                        file=finding["file"],
                        proposal=proposal,
                        feedback=answer,
                    ),
                    tools="Read,Grep,Glob",
                    schema=REVISION_SCHEMA,
                    resume=session_id,
                )
            except ClaudeUnavailable as error:
                print(f"{TAG} Couldn't get a revision: {error}. Keeping the previous proposal.")
                continue
            proposal = revision["proposed_docstring"]
            print(f"{TAG} {revision['message']}")
    return False


def format_tally(tally):
    return ", ".join(f"{count} {label}" for label, count in tally.items() if count)


def main():
    if skip_reason():
        return
    files = changed_python_files()
    if not files:
        return

    # AI policy: Claude only ever runs inside the dedicated sbx sandbox, never on the host.
    if os.environ.get("SANDBOX_VM_ID"):
        print(
            f"{TAG} Didn't run: this commit was made inside a sandbox; the check only "
            "runs for commits made from your host terminal."
        )
        return
    if shutil.which("sbx") is None:
        print(f"{TAG} Didn't run: sbx isn't installed or isn't on PATH.")
        return

    tty = open_tty()
    if tty is None:
        print(f"{TAG} Didn't run: no terminal available for the review prompts.")
        return

    try:
        ensure_sandbox()
        print(f"{TAG} Checking docstrings in {len(files)} changed Python file(s)...")
        verdict, session_id = run_claude(
            DETECT_PROMPT.format(
                sha=git_out("rev-parse", "HEAD"),
                files="\n".join(f"- {f}" for f in files),
            ),
            tools="Read,Grep,Glob,Bash",
            schema=FINDINGS_SCHEMA,
            allowed_tools="Bash(git show:*) Bash(git diff:*) Bash(git log:*)",
        )
    except ClaudeUnavailable as error:
        print(f"{TAG} Didn't run: {error}.")
        return

    findings = verdict["findings"]
    if verdict["status"] == "up_to_date":
        print(f"{TAG} Passed: {verdict['summary']}")
        return
    if not findings:
        print(f"{TAG} Didn't run: Claude reported stale docstrings but listed none.")
        return

    print(f"{TAG} {len(findings)} docstring(s) look stale after this commit:")
    list_findings(findings)
    applied = []
    tally = {"accepted": 0, "rejected": 0, "not located": 0}
    stopped = None
    try:
        answer = ask(tty, "Want Claude to propose replacements? [y/n] ")
        if COMMANDS.get(answer.lower()) != "y":
            print(f"{TAG} Left as is. They're listed above if you want to fix them.")
            return
        if review(findings, session_id, tty, applied, tally):
            stopped = "Quit"
    except (KeyboardInterrupt, EOFError):
        stopped = "Aborted"

    if stopped:
        print(f"\n{TAG} {stopped}.")
    if applied:
        print(
            f"{TAG} Updated {len(applied)} docstring(s) in your working tree; nothing was "
            "staged or committed. Review with `git diff`."
        )
    else:
        print(f"{TAG} Nothing was changed.")
    if any(tally.values()):
        print(f"{TAG} Done: {format_tally(tally)}.")


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{TAG} Aborted. Nothing was changed.")
    except Exception as error:
        print(f"{TAG} Didn't finish: unexpected error ({error}).")
    sys.exit(0)
