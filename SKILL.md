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

### 0. First thing: is this project already in flight?

**Do this before planning anything**, at the very start of the skill:

```bash
fleet adopt
```

It lists prior Claude Code *and* Codex sessions for this working directory. If
any come back, **ask the user before doing anything else** — something like:

> This project already has 3 earlier sessions here, most recently a Codex one
> ("Check project progress", 869 turns, yesterday). Want me to pick up from
> there, or start fresh?

If they want to continue:

```bash
fleet adopt <id> --as work                    # same CLI: resumes it for real
fleet adopt <id> --as work --handoff sonnet   # other CLI: carries the transcript across
```

Use the plain form when the session is on the CLI you are already running in —
that resumes the actual conversation. Use `--handoff` to cross CLIs: a Claude
session cannot literally be resumed inside Codex, so fleet extracts the
transcript, strips harness scaffolding, and opens a fresh session on the target
model seeded with it. Either way you end up with a named session you continue
with `--session work`.

If `fleet adopt` returns nothing, this is a new project — carry on to step 1.

### 1. Orient (only when something looks wrong)

```bash
fleet doctor --auth
```

`--auth` sends one trivial prompt per backend. Do this if a delegation fails
with a 401 or an auth error — it tells you which side needs a re-login instead
of leaving you guessing. Skip it on the happy path; it costs a round trip.

### 1b. What to keep, and what to send away

Delegations run **headless**. They do not open in your app, and the user cannot
watch them happen. That shapes what belongs where:

- **Keep in this conversation:** planning, research, weighing options, anything
  where the user's judgement changes the outcome. These are decisions, and they
  should be visible while they are being made.
- **Send to workers:** implementation, tests, mechanical refactors, audits —
  work with a definite goal that can be checked once it comes back.

Every delegation leaves a real, openable session behind. `fleet run` and
`fleet batch` print the command for each one:

```
ok   [haiku] b in 6.4s   open: claude --resume 7cb052d7-...
ok   [spark] a in 7.2s   open: codex resume 01a07b9b-...
```

Pass those on when the user asks what a worker actually did — they can read the
whole conversation. `fleet sessions` lists the same for named sessions.

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

### 2b. Carry your reasoning, not just your conclusions

A delegate is a fresh process. It gets an instruction with no idea why. Put the
settled decisions in the plan and they are prepended to every task:

```json
{
  "goal": "Add rate limiting to the public API",
  "context": "The API is behind a shared gateway; per-process limits would not hold.",
  "decisions": [
    "Token bucket, not a fixed window - bursts are expected and acceptable.",
    "Limits live in config, not in code.",
    "No new dependencies."
  ],
  "tasks": [ ... ]
}
```

Workers are told these are settled, and told to **stop and say so** rather than
silently choose differently if one turns out to be impossible. Set
`"inherit_decisions": false` on a task that genuinely should not see them.

Write down the constraints a reviewer would otherwise flag later. If you rejected
an approach, say which and why — that is the part a delegate cannot reconstruct.

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

## Draining the foreground queue

Delegations normally run headless. Sometimes the opposite is wanted: work that
runs **in this conversation**, where the user can watch the prompts, the tool
calls and the results as they happen. That is what the queue is for — an
orchestrator enqueues, and an attached session executes in the foreground.

### Attaching

```bash
fleet queue attach --worker "<a name for this session>" --poll 300
```

Then start the drain loop, matching the interval:

```
/loop 5m drain the fleet queue for this project
```

**One session per project.** A second `attach` is refused while the first is
still polling — two drainers would run the same task twice. If you are told a
worker is already attached, that is not an error to work around: it means the
work is already being picked up.

### Each tick

1. `fleet queue attach --worker "<same name>" --poll 300` — re-attach. The slot
   is held by re-attaching, not by a running process, so skipping this releases
   it after two missed polls.
2. `fleet queue next --json` — claims one task atomically. Two sessions can
   never get the same task.
3. **If a task comes back**, tell the user what you picked up, then **do the work
   here** — real tool calls in this conversation, not a headless delegation.
   Visibility is the entire point; `fleet run` would hide it again.
   - On long work call `fleet queue beat <id>` so the lease does not expire
     underneath you.
   - Finish with `fleet queue done <id> --summary "..."` or
     `fleet queue fail <id> --error "..."`. The summary is what the user sees in
     the completion notification, so write it for them, not for a log.
4. **If nothing is pending**, say so briefly and let the loop tick again.

### Why marking the outcome matters

A claim is a lease. If a task is never marked done, it returns to `pending` when
the lease expires and **runs a second time**. Always close it out — a failure
recorded honestly is better than a task silently repeating.

That same rule is the crash recovery: if this session dies mid-task, nothing
beats the lease, and the work returns to the queue by itself.

### Enqueuing

```bash
fleet enqueue "Add a /health endpoint with a smoke test" --label health
```

Re-enqueuing identical work in the same directory is refused rather than
duplicated. Pass `--key` to control that yourself, or `--priority` to jump the
line. `fleet queue list` shows what is waiting; `fleet queue status` shows
whether anyone is actually draining it.

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

## The task workflow (mandatory)

Every assigned task follows the same five steps. `fleet task` enforces them —
`fleet task status` exits non-zero until all four gates pass, so you can check
rather than assume.

```bash
fleet task start add-rate-limiting
```

That cuts a branch (`fleet/<slug>`), refuses to start on a dirty tree, and drops
an `understanding.md` template in `.fleet/tasks/<slug>/`.

**1. Branch.** Never work on `main`. `fleet task start` does this for you.

**2. Do the work**, delegating as usual with `fleet run` / `fleet batch`.

**3. Receipt — mandatory.** Every run writes `receipt.ipynb` automatically; attach
the relevant one to the task:

```bash
fleet task receipt add-rate-limiting
```

It is a real Jupyter notebook recording the goal, every delegation (model, tier,
prompt, response, duration, pass/fail), the branch and diffstat, and an empty
verification cell. **Fill the verification cell in** with what you actually ran
and what it printed. A receipt with no verification is a claim, not evidence.

**4. Review — by you, the orchestrator.** Read the diff yourself. Run the tests.
Then record it:

```bash
fleet task review add-rate-limiting --verdict pass --by fable --notes "..."
```

`--verdict changes` keeps the gate shut. Do not record `pass` because a delegate
said it was fine — record it because you checked.

**5. Understanding document — in human language.** Fill in
`.fleet/tasks/<slug>/understanding.md`: what was asked, what changed, why this
way, what could go wrong, how it was checked. Write it for a person who was not
here — no model names, no task ids, no jargon. The gate wants real prose, so an
untouched template will not pass.

**Then merge:**

```bash
fleet task status add-rate-limiting   # must exit 0
```

It prints the merge command once all four gates are green. If it does not,
something is genuinely missing — fix that rather than merging around it.

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
