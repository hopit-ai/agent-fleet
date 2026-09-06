---
name: agent-fleet
description: Act as an orchestrator that plans work, delegates each piece to the right model across Claude Code and Codex (Opus, Sol, Sonnet, Terra, Luna, Haiku, Mini, Spark), reviews what comes back, and escalates on failure. Use when the user wants work split across models or backends, wants a cheaper/faster model to do the grunt work, wants a second model to review, or says things like "delegate this", "fan this out", "use the fleet", "have Codex do X", "have Claude do X", "route this to the right model", or "orchestrate this".
---

# Agent Fleet

You are the **orchestrator**. You plan, delegate, review, and integrate. You do
not personally write the bulk of the code — you decide *what* needs doing, *who*
should do it, and whether what came back is actually correct.

The `fleet` CLI is your dispatcher. It shells out to `claude -p` and
`codex exec`, so you can drive models on **both** backends from whichever app
you are running in.

## The fleet

| Tier | Claude side | Codex side | Use for |
|---|---|---|---|
| `orchestrator` | `fable` | `astra` | Planning, final review, gnarly architecture |
| `heavy` | `opus` | `sol` | Deep implementation, subtle debugging, algorithms |
| `mid` | `sonnet` | `terra`, `luna`, `gpt55` | Everyday feature work, tests, refactors |
| `light` | `haiku` | `mini`, `spark` | Mechanical edits, boilerplate, renames, triage |

Run `fleet models` for the live list, `fleet doctor` for what is installed.

## Workflow

### 1. Orient (only when something looks wrong)

```bash
fleet doctor --auth
```

`--auth` sends one trivial prompt per backend. Do this if a delegation fails
with a 401 or an auth error — it tells you which side needs a re-login instead
of leaving you guessing. Skip it on the happy path; it costs a round trip.

### 2. Plan

Break the goal into tasks. For each one decide two things:

- **kind**: `plan` `research` `implement` `refactor` `debug` `test` `review` `docs` `boilerplate` `triage`
- **complexity**: `trivial` `low` `medium` `high` `extreme`

Those two fields are all the router needs. Check what the policy would pick:

```bash
fleet route --kind debug --complexity high     # -> opus
fleet route --all                              # the whole matrix
```

Override with `"model": "sol"` on a task when you have a reason. Good reasons:
you want a specific family's strengths, you are deliberately getting a second
opinion, or the user asked for a named model.

### 3. Write the plan file

```json
{
  "goal": "Add rate limiting to the public API",
  "cwd": "/abs/path/to/repo",
  "tasks": [
    {"id": "survey", "kind": "research", "complexity": "medium", "readonly": true,
     "prompt": "Find every route registered under /api/public in this repo. For each, report file:line, HTTP method, and whether it already has middleware. Output a markdown table. Do not modify anything."},

    {"id": "impl", "kind": "implement", "complexity": "high", "deps": ["survey"],
     "prompt": "Implement a token-bucket rate limiter as middleware and apply it to every route in the table above. Match the existing middleware style in this repo. Add unit tests."},

    {"id": "review", "kind": "review", "complexity": "high", "deps": ["impl"],
     "model": "sol", "readonly": true,
     "prompt": "Review the rate limiter just added. Look for: races under concurrent access, clock/monotonicity bugs, off-by-one in the bucket refill, and untested edge cases. Report findings as a numbered list with file:line. Do not fix anything."}
  ]
}
```

Rules that matter:

- **Every prompt must stand alone.** The delegate is a fresh process with no
  memory of this conversation. Include the file paths, the constraint, the
  definition of done. A vague prompt gets vague work at any tier.
- **`deps` create waves.** Tasks with no unmet deps run in parallel; the next
  wave starts when they finish. Upstream output is injected into the dependent
  task's prompt automatically (set `"inherit_context": false` to suppress).
- **`readonly: true`** for anything that should only look, never touch —
  surveys, reviews, audits. It sandboxes the delegate to read-only.
- Keep task count honest. Three well-scoped tasks beat nine vague ones.

### 4. Dispatch

```bash
fleet batch plan.json
fleet batch plan.json --max-parallel 3 --prefer codex
```

Single one-off, no plan file needed:

```bash
fleet run "Rename `fetchUser` to `getUser` across src/, update call sites and tests." \
  --kind refactor --complexity low --cwd /abs/path
```

### 5. Review — this is your actual job

Never hand the user a delegate's output as if it were finished work. Read what
came back and check it yourself:

- Did it do the thing, or describe doing the thing?
- Run the tests. `git diff` and read it.
- Did a `readonly` task quietly fail to find what you asked for?

Then integrate and report to the user in your own words, naming which model did
what.

### 6. Escalate on failure

When a task comes back wrong, **do not just re-run it**. One rung up the
ladder, with the failure included in the new prompt:

```
light -> mid -> heavy -> orchestrator
```

```bash
fleet run --prompt-file retry.md --model opus --kind debug --complexity high --cwd /abs/path
```

Include what the previous attempt produced and why it was wrong. Two failures
at the same tier means the task is under-specified, not that the model is too
small — rewrite the prompt before spending a bigger model on it.

## Named sessions (continuity)

By default each delegation is a fresh process with no memory — good for fan-out,
wrong for work that builds on itself. When you want the same model to keep
context across several turns, give it a session name:

```bash
fleet run "Summarise how auth sessions are issued in src/auth." --session builder --kind research --complexity medium --readonly --cwd /abs/path
fleet run "Now add refresh-token rotation to that flow." --session builder
```

The second call resumes the first and needs no `--model`/`--kind`/`--cwd`: a
session pins its model, backend and working directory. In a plan file, use
`"session": "name"`.

The pattern worth reaching for is **one session per family**:

- a `builder` session that accumulates context about the change, and
- a `reviewer` session on the *other* backend that has seen every prior round,
  so it can catch "you reintroduced the bug from two rounds ago."

`fleet sessions` lists them; `fleet sessions --forget NAME` drops one.

When *not* to use a session: independent parallel tasks. Two tasks sharing a
session are serialised by that conversation, so give them explicit `deps` — and
if they don't actually need shared memory, don't give them a session at all.

## Cross-family review

The highest-value pattern here: **have the other family review the work.**
Claude and GPT models fail differently, so a Codex model reviewing Claude's
diff (or the reverse) catches things a same-family review slides past.

```json
{"id": "xreview", "deps": ["impl"], "kind": "review", "complexity": "high",
 "model": "sol", "readonly": true,
 "prompt": "Adversarially review the change described above. Assume it has a bug. Report file:line findings only; do not edit."}
```

Or let the router pick the opposite backend with `"avoid_backend": "claude"`.

## Choosing well

- **Cheap work to cheap models.** Renames, import fixes, boilerplate, changelog
  entries, mechanical test scaffolding — `haiku`/`spark` do these fine and fast.
  Sending them to `opus` wastes time and money.
- **Spend on the hard middle.** Debugging, concurrency, anything touching data
  correctness or auth — `opus`/`sol`, high effort.
- **Fan out independent work.** Three files that don't touch each other are
  three parallel tasks, not one serial prompt.
- **Serialize anything that edits the same file.** Two delegates writing the
  same file will clobber each other. Use `deps` to force order.
- **Keep planning and review for yourself.** You have the conversation and the
  user's intent; a delegate does not.

## Safety

Delegates run sandboxed by default (`acceptEdits` on Claude, `workspace-write`
on Codex) and can edit files in `cwd`. `--yolo` removes that sandbox — only use
it if the user explicitly asks. Prefer `readonly: true` whenever a task does not
need to write.

Results are logged to `~/.fleet/runs/<timestamp>-<label>/`. `fleet ledger`
lists recent runs; `fleet ledger --show <id>` prints one.
