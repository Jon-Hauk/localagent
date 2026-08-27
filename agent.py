#!/usr/bin/env python3
"""A local coding harness for Ollama models, with a human gate on every write.

The model proposes; you dispose. Nothing touches disk and nothing runs until
you approve it, one proposal at a time. That is the whole point: a local agent
is a competent junior, not a colleague, so the review step is not optional.

Pure standard library on purpose. No pip, no venv, no compiled extensions -
so it runs under the system Python that Windows already trusts, instead of
fighting Smart App Control over an unsigned shim.

    python localagent/agent.py                 # default model
    python localagent/agent.py --model qwen3-coder:30b
    python localagent/agent.py --add script.py --add notes.md

In the prompt:
    /add <path>     put a file in the model's context (re-read fresh each turn)
    /drop <path>    remove one
    /files          list what the model can currently see
    /model <name>   switch model mid-session
    /workspace      show where approved files are saved
    /clear          forget the conversation (keeps added files)
    /help  /quit
    anything else   a request to the model

When the model proposes code, you get a per-file gate:
    [s] save   [r] run (save first)   [f] feedback (revise)   [d] discard
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

OLLAMA = "http://127.0.0.1:11434/api/chat"
DEFAULT_MODEL = "qwen2.5-coder:14b"
# Small models drift without a large window once files are in context. This is
# the single setting that most affects whether the loop stays coherent.
NUM_CTX = 16384

SYSTEM_PROMPT = """\
You are a coding assistant driven from a terminal. The human reviews and
approves every file before it is written or run, so propose freely but do not
assume anything you write takes effect.

When you propose a file or script, emit it as a fenced code block whose FIRST
line is a comment naming the target path, exactly like:

```python
# file: tools/cleanup.py
...code...
```

Use the right comment syntax for the language (# for python/sh, // for js,
<!-- --> for html). One file per fenced block; use several blocks for several
files. Keep prose brief and put it before the blocks, never inside them. If the
human gives feedback on a proposal, return the full revised file, not a diff.
"""

# fence lang or file extension -> how to run it. Absence means save-only.
RUNNERS = {
    "python": [sys.executable],
    "py": [sys.executable],
    "sh": ["bash"],
    "bash": ["bash"],
    "ps1": ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"],
    "powershell": ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"],
}

FENCE = re.compile(r"```([\w+-]*)\n(.*?)```", re.DOTALL)
FILE_HINT = re.compile(r"file:\s*(\S+)")


class Colors:
    DIM = "\033[2m"
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    RED = "\033[31m"
    OFF = "\033[0m"


C = Colors()


def say(msg: str, color: str = "") -> None:
    print(f"{color}{msg}{C.OFF}" if color else msg)


def ask(prompt: str) -> str:
    """input() that treats EOF and Ctrl-C as 'no' rather than a traceback.

    Every caller here is a confirmation, so the safe reading of "the operator
    went away or the pipe ended" is decline, never proceed.
    """
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


class Proposal:
    """One fenced code block the model wants to become a file."""

    def __init__(self, lang: str, body: str, index: int):
        self.lang = lang.lower()
        # Pull `file: <path>` out of the first two lines if the model gave one,
        # and strip that marker line from the body. It is addressed to this
        # harness, not part of the file: harmless noise in Python, outright
        # corruption in JSON or YAML.
        self.path = None
        lines = body.splitlines()
        for i, line in enumerate(lines[:2]):
            m = FILE_HINT.search(line)
            if m:
                self.path = m.group(1)
                del lines[i]
                if lines and not lines[0].strip():
                    del lines[0]
                break
        self.body = "\n".join(lines).rstrip("\n") + "\n"
        if not self.path:
            ext = self.lang if self.lang else "txt"
            self.path = f"proposal_{index}.{ext}"

    @property
    def runner(self):
        key = self.lang or Path(self.path).suffix.lstrip(".")
        return RUNNERS.get(key)


def parse_proposals(text: str) -> list[Proposal]:
    out = []
    for i, (lang, body) in enumerate(FENCE.findall(text), 1):
        out.append(Proposal(lang, body, i))
    return out


def chat_stream(model: str, messages: list[dict]):
    """Stream assistant text from Ollama, yielding chunks as they arrive."""
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"num_ctx": NUM_CTX},
        }
    ).encode()
    req = urllib.request.Request(
        OLLAMA, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        # S310: OLLAMA is a fixed http://127.0.0.1 constant defined above,
        # never user- or model-supplied, so no scheme smuggling is possible.
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw in resp:
                raw = raw.strip()
                if not raw:
                    continue
                obj = json.loads(raw)
                if obj.get("done"):
                    break
                chunk = obj.get("message", {}).get("content", "")
                if chunk:
                    yield chunk
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"cannot reach Ollama at {OLLAMA} ({e.reason}). Is the Ollama app running?"
        ) from e


def build_context(added: dict[str, Path]) -> str:
    """Fresh snapshot of every added file, injected each turn so edits show."""
    if not added:
        return ""
    parts = ["Files currently in context (read-only unless you propose changes):\n"]
    for name, path in added.items():
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            content = f"<<could not read: {e}>>"
        parts.append(f"\n--- {name} ---\n{content}")
    return "\n".join(parts)


def review(prop: Proposal, workspace: Path, added: dict[str, Path]) -> str | None:
    """Human gate for one proposal. Returns 'feedback text' to revise, else None."""
    say(f"\n{'=' * 60}", C.DIM)
    say(f"proposed: {prop.path}" + ("" if prop.runner else "  (save-only)"), C.BOLD)
    say("=" * 60, C.DIM)

    target = Path(prop.path)
    original = added.get(prop.path) or (
        added.get(target.name) if target.name in added else None
    )

    # If this overwrites a file already in context, show a diff instead of dumping.
    if original and original.exists():
        import difflib

        old = original.read_text(encoding="utf-8", errors="replace").splitlines()
        new = prop.body.splitlines()
        diff = list(
            difflib.unified_diff(
                old, new, lineterm="", n=2, fromfile="current", tofile="proposed"
            )
        )
        if diff:
            for line in diff:
                col = (
                    C.GREEN
                    if line.startswith("+")
                    else C.RED
                    if line.startswith("-")
                    else C.DIM
                )
                say(line, col)
        else:
            say("(no change from the current file)", C.DIM)
    else:
        for n, line in enumerate(prop.body.splitlines(), 1):
            print(f"{C.DIM}{n:>3}{C.OFF} {line}")

    while True:
        say("\n[s]ave  [r]un  [f]eedback  [d]iscard", C.CYAN)
        choice = ask("> ").lower()

        if choice in ("d", ""):
            say("discarded.", C.DIM)
            return None

        if choice == "f":
            fb = ask("what should change? ")
            if fb:
                return fb
            continue

        if choice in ("s", "r"):
            # Save target: overwrite the in-context original if that is what
            # this is, otherwise land it in the workspace under its basename.
            if original:
                dest = original
            else:
                dest = workspace / Path(prop.path).name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if (
                dest.exists()
                and not original
                and ask(f"{dest} exists. overwrite? [y/N] ").lower() != "y"
            ):
                continue
            dest.write_text(prop.body, encoding="utf-8")
            say(f"saved -> {dest}", C.GREEN)

            if choice == "r":
                if not prop.runner:
                    say(f"no runner for .{prop.lang}; saved only.", C.YELLOW)
                    return None
                cmd = [*prop.runner, str(dest)]
                say(f"\nabout to run: {' '.join(cmd)}", C.YELLOW)
                if ask("run it? [y/N] ").lower() != "y":
                    say("not run.", C.DIM)
                    return None
                say(f"{'-' * 60}\n(running in {dest.parent})", C.DIM)
                try:
                    # S603: yes - this executes model-generated code, which is
                    # the entire purpose of the tool. The control is procedural,
                    # not technical: the operator has already read the source in
                    # the gate above, chosen to save it, seen the exact argv
                    # printed, and answered a second confirmation. cmd[0] comes
                    # from the RUNNERS table, never from the model. Nothing here
                    # goes through a shell.
                    r = subprocess.run(
                        cmd,
                        check=False,
                        cwd=dest.parent,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    if r.stdout:
                        print(r.stdout, end="")
                    if r.stderr:
                        say(r.stderr, C.RED)
                    say(f"(exit {r.returncode})", C.DIM if r.returncode == 0 else C.RED)
                except subprocess.TimeoutExpired:
                    say("timed out after 120s.", C.RED)
                except OSError as e:
                    say(f"could not run: {e}", C.RED)
            return None

        say("pick s, r, f, or d.", C.YELLOW)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Local coding harness for Ollama, human-gated."
    )
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument(
        "--add",
        action="append",
        default=[],
        metavar="PATH",
        help="file to put in context",
    )
    ap.add_argument(
        "--workspace",
        default="localagent-workspace",
        help="where approved files are saved",
    )
    args = ap.parse_args()

    model = args.model
    workspace = Path(args.workspace).resolve()
    added: dict[str, Path] = {}
    for a in args.add:
        p = Path(a)
        if p.exists():
            added[a] = p
        else:
            say(f"skip --add {a}: not found", C.YELLOW)

    history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    say(f"\n  localagent  {C.DIM}model={model}  workspace={workspace}{C.OFF}", C.BOLD)
    say(
        f"  {len(added)} file(s) in context. /help for commands, /quit to leave.\n",
        C.DIM,
    )

    while True:
        try:
            line = input(f"{C.GREEN}»{C.OFF} ").strip()
        except (EOFError, KeyboardInterrupt):
            say("\nbye.", C.DIM)
            return 0
        if not line:
            continue

        if line.startswith("/"):
            cmd, _, arg = line[1:].partition(" ")
            arg = arg.strip()
            if cmd in ("quit", "q", "exit"):
                say("bye.", C.DIM)
                return 0
            elif cmd == "help":
                say(__doc__)
            elif cmd == "add":
                p = Path(arg)
                if p.exists():
                    added[arg] = p
                    say(f"added {arg}", C.GREEN)
                else:
                    say(f"not found: {arg}", C.RED)
            elif cmd == "drop":
                if added.pop(arg, None):
                    say(f"dropped {arg}", C.DIM)
                else:
                    say(f"not in context: {arg}", C.YELLOW)
            elif cmd == "files":
                if added:
                    for name in added:
                        say(f"  {name}", C.DIM)
                else:
                    say("  (none)", C.DIM)
            elif cmd == "model":
                if arg:
                    model = arg
                    say(f"model -> {model}", C.GREEN)
                else:
                    say(f"model is {model}", C.DIM)
            elif cmd == "workspace":
                say(f"  {workspace}", C.DIM)
            elif cmd == "clear":
                history = [{"role": "system", "content": SYSTEM_PROMPT}]
                say("conversation cleared (files kept).", C.DIM)
            else:
                say(f"unknown command: /{cmd}", C.YELLOW)
            continue

        # A request to the model. Inject fresh file context ahead of it.
        ctx = build_context(added)
        user_msg = f"{ctx}\n\n{line}" if ctx else line
        history.append({"role": "user", "content": user_msg})

        say("", "")
        collected = []
        try:
            for chunk in chat_stream(model, history):
                print(chunk, end="", flush=True)
                collected.append(chunk)
        except RuntimeError as e:
            say(f"\n{e}", C.RED)
            history.pop()  # do not keep a turn that never got an answer
            continue
        print()
        reply = "".join(collected)
        history.append({"role": "assistant", "content": reply})

        # Gate each proposed file. Feedback re-asks the model and re-gates.
        proposals = parse_proposals(reply)
        if not proposals:
            continue
        say(f"\n{len(proposals)} proposal(s).", C.DIM)
        for prop in proposals:
            fb = review(prop, workspace, added)
            if fb:
                history.append({"role": "user", "content": f"Revise {prop.path}: {fb}"})
                say("\nrevising...\n", C.DIM)
                collected = []
                try:
                    for chunk in chat_stream(model, history):
                        print(chunk, end="", flush=True)
                        collected.append(chunk)
                except RuntimeError as e:
                    say(f"\n{e}", C.RED)
                    history.pop()
                    continue
                print()
                revised = "".join(collected)
                history.append({"role": "assistant", "content": revised})
                for rp in parse_proposals(revised):
                    review(rp, workspace, added)


if __name__ == "__main__":
    raise SystemExit(main())
