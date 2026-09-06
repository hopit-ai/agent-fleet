# Contributing

Thanks for taking a look. This is a small, dependency-free tool and the aim is
to keep it that way.

## Ground rules

- **Python 3.9+, standard library only.** No runtime dependencies, ever. If a
  change needs a package, it probably belongs in a separate tool.
- **`roster.json` is config, not code.** Adding or reweighting a model should
  never require touching `bin/fleet`. If it does, that's a bug in the design.
- **Tests must run without any backend installed.** CI has neither `claude` nor
  `codex` and no credentials. Fake backend availability by monkeypatching
  `fleet.backend_bin` — see `tests/test_fleet.py`.

## Running tests

```bash
python -m unittest discover -s tests -v
```

## Adding a model

Add an entry to `models` in `roster.json` with its `backend`, provider `id`, and
`tier`. If it should become a tier default, update `tier_defaults`. The roster
integrity tests will tell you if you've left something inconsistent.

## Adding a backend

1. Add it to `backends` in `roster.json`, with `bin` plus any `bin_fallbacks`
   (paths may use `~`).
2. Add a branch to `build_cmd()` in `bin/fleet` that returns the argv for a
   single non-interactive run. It must support a read-only mode and a sandboxed
   write mode.
3. Add a branch to `extract_text()` if the CLI's output needs parsing.
4. Add command-building tests covering readonly, default, and `--yolo`.

## Scope

Good fits: more backends, better routing heuristics, cost/latency reporting,
retry and escalation policy, structured output.

Poor fits: anything that turns this into a long-running service, a web UI, or a
framework. It is a dispatcher.
