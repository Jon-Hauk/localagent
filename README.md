# localagent

A coding harness for local Ollama models where **you approve every write and
every execution**. The model proposes; nothing reaches disk or runs until you
say so, one file at a time.

```bash
python localagent/agent.py
python localagent/agent.py --model qwen3-coder:30b --add fieldkit/checks/Test-NetworkHealth.ps1
```

Needs the Ollama app running (it serves `127.0.0.1:11434`). Nothing else -
no pip install, no venv, no compiled extensions.

## Why it is plain stdlib

Smart App Control is enabled on this machine and blocks unsigned local
executables, which is what killed `aider.exe` and uv's copied `python.exe`.
Standard library only means this runs under the system Python that Windows
already trusts, so there is no security control to argue with. That constraint
is worth keeping: it is why this file imports nothing that is not in the
standard library.

## The loop

Ask for something. The model answers, and any fenced code block whose first
line names a path becomes a **proposal**:

```
[s]ave  [r]un  [f]eedback  [d]iscard
```

- **save** - writes it. If the file was `/add`ed, it overwrites in place;
  otherwise it lands in the workspace directory (`--workspace`, default
  `./localagent-workspace`) so nothing is written where you did not expect.
- **run** - saves, shows you the exact command, then asks a *second* time
  before executing. 120s timeout, output captured. Python, sh/bash and
  PowerShell have runners; anything else is save-only.
- **feedback** - type what should change; the model returns a full revision and
  you gate that one too. This is the iterate step, and it is the one worth
  using - small models rarely land it first try.
- **discard** - nothing happens.

Files you `/add` are re-read fresh on every turn, so if you edit one in another
window the model sees the current version, not a stale snapshot. When a
proposal targets an added file you get a colored diff instead of a wall of
code.

## Commands

| | |
|---|---|
| `/add <path>` | put a file in context |
| `/drop <path>` | remove one |
| `/files` | what the model can see |
| `/model <name>` | switch model mid-session |
| `/workspace` | where approved files land |
| `/clear` | forget the conversation, keep the files |
| `/help` `/quit` | |

## Model notes

`qwen2.5-coder:14b` (9 GB) is fully GPU-resident on a 12 GB card and quick.
`qwen3-coder:30b` (18 GB) is a mixture-of-experts that spills into system RAM -
slower per turn, better at multi-step work, and worth switching to with
`/model` when a task gets structural.

If you build a Modelfile `FROM` one of these, do not set `TEMPLATE`. Ollama
0.32 ships a built-in `RENDERER qwen3-coder` / `PARSER qwen3-coder` pair that
handles tool calls server-side, and a `TEMPLATE {{ .Prompt }}` line overrides
it - the tools block is never injected and `tool_calls` comes back null. This
harness parses fences and does not use tool calls, so it survives that; a
tool-driven harness pointed at the same model does nothing at all and looks
broken for no visible reason. Verified 2026-08-24: identical prompt, same
blob, `qwen3-coder:30b` returned a structured call and a derived model with
that line returned none.

Context is set to 16K (`NUM_CTX`). Adding several large files will crowd it and
the model starts drifting; prefer a few focused files over a directory.

Calibration, honestly: a local 14B is a competent junior. It is good at
single-file work, boilerplate, tests and refactors, and unreliable at long
multi-step reasoning. The approval gate is not ceremony - it is the thing that
makes the tool safe to use.

## License

Apache-2.0. See [LICENSE](LICENSE).

Chosen over MIT for the explicit patent grant, which is what makes this
adoptable inside a company rather than only readable. It disclaims warranty,
which matters more than usual here: this is a harness that runs model-proposed
edits against your files, and the approval gate is the only thing between a
suggestion and a write.
