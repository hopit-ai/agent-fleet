# agent-fleet

[![ci](https://github.com/hopit-ai/agent-fleet/actions/workflows/ci.yml/badge.svg)](https://github.com/hopit-ai/agent-fleet/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**One orchestrator, both CLIs, every model.**

Run it inside [Claude Code](https://claude.com/claude-code) or inside Codex.
Either way, the app you're in becomes the orchestrator: it plans the work, hands
each piece to whichever model actually fits it — *on either backend* — reviews
what comes back, and escalates when something fails.

```
   Claude Code  ──┐                    ┌──►  claude -p   ──►  opus · sonnet · haiku
                  ├──►   fleet   ──────┤
   Codex        ──┘                    └──►  codex exec  ──►  sol · terra · luna · mini · spark
```

No daemon, no server, no SDK. `fleet` is a single dependency-free Python script
that shells out to CLIs you already have.

## Why

Three things fall out of this that you don't get from one model in one app:

- **Cost and latency, graded.** Renames, boilerplate and changelog entries go to
  a small fast model. Concurrency bugs and data-correctness work go to a big one.
  You stop paying frontier prices for mechanical edits.
- **Cross-family review.** Different model families fail in *different places*, so
  a GPT model reviewing a Claude diff catches things a same-family review nods
  past — and the reverse. This is the highest-value pattern here.
- **Real parallelism.** Independent tasks run at the same time in separate
  processes, each with its own context window.
- **Continuity across tools.** A project started in one CLI can be picked up in
  the other, carrying the actual conversation rather than a re-explanation.
- **Evidence by default.** Every run writes a notebook receipt recording who did
  what, with which prompt, and what changed.

A worked example from the repo's own development: a mid-tier Claude model wrote a
token-bucket rate limiter, a heavy Codex model reviewed it and correctly found
missing input validation — then *overstated* the impact, claiming a general
capacity bypass where the code made it a narrow same-tick race. Both models were
useful. Neither was authoritative. **Read the diff.**

## Requirements

- Python 3.9+ (standard library only — no pip install)
- At least one of:
  - [Claude Code](https://claude.com/claude-code) (`claude` on your PATH)
  - Codex CLI (`codex` on your PATH; on macOS it also ships inside the ChatGPT
    app and `fleet` finds it there automatically)

Having both is the point, but it degrades gracefully to one.

## Install

```bash
git clone https://github.com/hopit-ai/agent-fleet.git
cd agent-fleet
./install.sh
```

It writes five files and prints all of them, plus the `rm -rf` line to undo it:

| Path | What |
|---|---|
| `~/.fleet/bin/fleet` | the dispatcher |
| `~/.fleet/roster.json` | models + routing policy — **edit this to change behaviour** |
| `~/.local/bin/fleet` | symlink onto your PATH |
| `~/.claude/skills/agent-fleet/SKILL.md` | orchestrator playbook for Claude Code |
| `~/.codex/skills/agent-fleet/SKILL.md` | the same playbook for Codex |

Skill directories are only written if the corresponding app is already set up.

## Quickstart

Check what resolves on your machine:

```bash
fleet doctor
```

`doctor` only checks that binaries exist. To confirm both CLIs are actually
*authenticated*, send one trivial prompt per backend:

```bash
fleet doctor --auth
```

If this project has been worked on before — in *either* CLI — pick that up rather
than starting cold:

```bash
fleet adopt
```

See [Picking up an existing project](#picking-up-an-existing-project).

Delegate a single task — routing is automatic from `--kind` and `--complexity`:

```bash
fleet run "Rename fetchUser to getUser across src/, update call sites and tests." \
  --kind refactor --complexity low --cwd ~/code/myrepo
```

Or just talk to whichever app you're in — the skill triggers on phrases like
*"use the fleet to…"*, *"delegate this"*, *"have Codex review it"*:

> use the fleet to add retry logic to the API client, and have a GPT model review it

## How routing works

A plain lookup — `(kind, complexity) → tier → model`. No magic, no LLM call.

| Tier | Claude | Codex | For |
|---|---|---|---|
| `orchestrator` | `fable` | `astra` | planning, final review, hard architecture |
| `heavy` | `opus` | `sol` | deep implementation, subtle debugging |
| `mid` | `sonnet` | `terra` `luna` `gpt55` | everyday features, tests, refactors |
| `light` | `haiku` | `mini` `spark` | boilerplate, renames, triage |

Inspect the whole matrix (works before you've installed any backend):

```bash
fleet route --all
```

Ask about one case:

```bash
fleet route --kind debug --complexity high --prefer codex
```

All of it lives in [`roster.json`](roster.json) — adding a model or reweighting
the policy never requires touching code.

## Plan files

Real work goes in a plan. See [`examples/`](examples) for complete ones.

```json
{
  "goal": "Add rate limiting to the public API",
  "cwd": "/absolute/path/to/repo",
  "tasks": [
    {"id": "survey", "kind": "research", "complexity": "medium", "readonly": true,
     "prompt": "List every route under /api/public with file:line. Markdown table. Read only."},

    {"id": "impl", "kind": "implement", "complexity": "high", "deps": ["survey"],
     "prompt": "Implement token-bucket rate limiting on those routes. Match existing middleware style. Add tests."},

    {"id": "xreview", "kind": "review", "complexity": "high", "deps": ["impl"],
     "avoid_backend": "claude", "readonly": true,
     "prompt": "Adversarially review that change. Races, clock choice, off-by-one refill, unvalidated inputs. file:line findings only."}
  ]
}
```

```bash
fleet batch plan.json
```

Tasks with no unmet `deps` run **in parallel**; the next wave starts when they
finish. Upstream output is injected into dependent prompts automatically.

Three rules decide whether this works well:

- **Every prompt must stand alone.** The delegate is a fresh process with no
  memory of your conversation. Paths, constraints, definition of done — all in
  the prompt. A vague prompt gets vague work at any tier.
- **Serialize anything touching the same file.** Two delegates writing one file
  will clobber each other. Use `deps` to force order.
- **Use `readonly` liberally.** Surveys, reviews and audits should never hold a
  write handle.

### Task fields

| Field | Meaning |
|---|---|
| `id` | task name, used by `deps` (auto-assigned if omitted) |
| `prompt` | the instruction — must be self-contained |
| `kind` | `plan` `research` `implement` `refactor` `debug` `test` `review` `docs` `boilerplate` `triage` |
| `complexity` | `trivial` `low` `medium` `high` `extreme` |
| `deps` | task ids that must finish first |
| `model` | force a specific model, bypassing routing |
| `prefer` / `avoid_backend` | steer routing toward or away from a backend |
| `readonly` | sandbox to read-only |
| `effort` | override reasoning effort |
| `cwd` | per-task working directory |
| `inherit_context` | set `false` to stop upstream output being injected |
| `session` | named persistent session — see [Named sessions](#named-sessions) |

## CLI

| Command | Does |
|---|---|
| `fleet doctor [--auth]` | what's installed; `--auth` also verifies credentials |
| `fleet models [--json]` | the roster |
| `fleet route [--all]` | ask the policy which model fits |
| `fleet run <prompt>` | delegate one task |
| `fleet batch <plan.json>` | dependency-ordered parallel fan-out |
| `fleet adopt` | list prior Claude Code / Codex sessions here; adopt or hand one off |
| `fleet sessions` | list named sessions; `--forget NAME` / `--forget-all` |
| `fleet task <cmd>` | branch → receipt → review → understanding → merge |
| `fleet receipt` | write a notebook receipt for a run |
| `fleet ledger [--show ID]` | past runs |

Every run is recorded under `~/.fleet/runs/<timestamp>-<label>/` with the plan,
each task's JSON result, and raw output.

## Named sessions

By default every delegation is a **fresh process with no memory** — that's what
makes fan-out safe and parallel. But some work is a conversation: you want the
same model to keep building on what it already did.

A named session is a persistent conversation with one model on one backend:

```bash
fleet run "Read src/auth and summarise how sessions are issued." \
  --session auth-work --kind research --complexity medium --readonly --cwd ~/code/myrepo
```

```bash
fleet run "Now add refresh-token rotation to that flow." --session auth-work
```

The second call resumes the first. It needs no `--model`, `--kind` or `--cwd` —
**a session pins its model, backend and working directory**, because a
conversation can't be continued somewhere it never happened. Trying to resume
one as a different model is refused with an explanation rather than silently
starting over.

Run two at once to keep a worker on each side, each with its own memory:

```bash
fleet sessions
```

```
NAME           MODEL    BACKEND TURNS  LAST USED            CWD
builder        sonnet   claude  4      2026-09-06T07:25:02  /Users/you/code/myrepo
reviewer       sol      codex   3      2026-09-06T07:25:09  /Users/you/code/myrepo
```

That pairing is the useful one: a `builder` session that accumulates context
about the change, and a `reviewer` session on the *other* family that has seen
every previous round and can say "you reintroduced the bug from two rounds ago."

Sessions work in plan files too, via `"session": "name"`. Note that two tasks
sharing a session are serialised by that session's conversation — give them
`deps` so the ordering is explicit rather than accidental.

To start a named session from work that already happened — in this CLI or the
other one — see [Picking up an existing project](#picking-up-an-existing-project).

Under the hood: `claude --session-id/--resume` and `codex exec resume`. Session
ids are recorded in `~/.fleet/sessions.json`; `fleet sessions --forget NAME`
drops one. Forgetting a session doesn't delete the underlying conversation, it
just stops fleet tracking it.

## Picking up an existing project

Both CLIs keep transcripts on disk. `fleet adopt` finds the ones for your current
directory — from **either** CLI — so a project already in flight can be continued
rather than re-explained:

```bash
fleet adopt
```

```
  #   CLI     SESSION ID                             TURNS  UPDATED              TITLE
  1   codex   01a06ade-18a3-7e71-a653-c79efc68b154   869    2026-09-07T04:53:53  Check project progress
  2   claude  a38e921b-29f9-4919-b1bf-de00cddb6f4d   250    2026-09-07T04:36:02  Self-distillation papers review
```

```bash
fleet adopt 01a06ade --as work                    # same CLI - resumes it for real
fleet adopt a38e921b --as work --handoff terra    # other CLI - carries the transcript
```

A Claude session cannot literally be resumed inside Codex, so `--handoff` extracts
the transcript, strips harness scaffolding (plugin lists, context blocks), budgets
it to fit, and opens a fresh session on the target model seeded with it. The model
replies with where the work stands before touching anything.

The orchestrator skill runs `fleet adopt` at startup and asks you before assuming
a project is new.

## The task workflow

For teams that want every change to arrive with evidence. `fleet task` enforces
four gates and exits non-zero until all of them pass:

```bash
fleet task start add-rate-limiting     # cuts fleet/add-rate-limiting, refuses a dirty tree
#   ... delegate the work as usual ...
fleet task receipt add-rate-limiting   # attach the notebook receipt
fleet task review  add-rate-limiting --verdict pass --by fable
#   ... fill in .fleet/tasks/<slug>/understanding.md ...
fleet task status  add-rate-limiting   # exit 0 only when all four are green
```

```
  [x] branch         on fleet/add-rate-limiting
  [x] receipt        receipt.ipynb (9 cells)
  [x] review         pass by fable
  [ ] understanding  88 chars of prose

NOT ready to merge - open gates: understanding
```

**The receipt** is a real `.ipynb` (written as plain JSON — no notebook libraries
needed) holding the goal, every delegation with its prompt and response, the
branch and diffstat, and a verification cell for you to fill with what you
actually ran. Every `fleet run` and `fleet batch` writes one automatically.

**The understanding document** is prose for a person who was not there: what was
asked, what changed, why this way, what could go wrong, how it was checked. The
gate rejects an untouched template.

## Safety

Delegates are sandboxed by default and scoped to the task's `cwd`:
`--permission-mode acceptEdits` on Claude, `-s workspace-write` on Codex.
`readonly: true` drops them to read-only, and **readonly always wins over
`--yolo`**. `--yolo` removes the sandbox entirely — opt in deliberately.

`fleet` itself makes no network calls; the CLIs it invokes do.

## Configuration

| Variable | Effect |
|---|---|
| `FLEET_HOME` | where runs and the roster live (default `~/.fleet`) |
| `FLEET_ROSTER` | use a specific roster file |
| `FLEET_CLAUDE_BIN` / `FLEET_CODEX_BIN` | pin a backend executable |

Roster lookup order: `$FLEET_ROSTER`, `./.fleet/roster.json`,
`$FLEET_HOME/roster.json`, then the copy beside the script — so a repo can carry
its own routing policy.

## A note on model names

The default roster reflects the models available at the time of writing, verified
against Claude Code `2.1.263` and codex-cli `0.153.3`. Providers rename and retire
models. If `fleet doctor` shows something unreachable, edit `roster.json` — that's
what it's for. Nothing about the design depends on a particular model existing.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Python 3.9+, stdlib only, and tests must
pass with no backends installed and no credentials.

```bash
python -m unittest discover -s tests -v
```

## License

MIT — see [LICENSE](LICENSE).
