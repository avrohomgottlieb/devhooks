# devhooks

A personal collection of git hooks that call out to an LLM to enhance development workflow. Each
hook is installed through the [pre-commit](https://pre-commit.com) framework, via
`.pre-commit-hooks.yaml` — the same manifest mechanism any repo uses to make its hooks available
to others (it's how `pre-commit/pre-commit-hooks`, `psf/black`, and `biomejs/pre-commit` all work,
and why a consuming repo's `.pre-commit-config.yaml` can reference this repo by URL + `id:`). A
consuming repo gets a devhook through the install flow it already uses for its other hooks.

**Not tied to any one model or provider.** The goal is to support local models (e.g. via Ollama)
as a default, with an optional escalation path to a hosted model (e.g. Claude) for harder cases.
`docstring_check` (below) currently only implements a Claude backend; generalizing the backend to
other models is an open item, not yet designed.

## Hard constraints every hook here follows

- **The LLM itself never touches git history:** no commit, amend, reset, staging, or push via its
  own tool calls, autonomously decided.
- **Nuance:** this is a constraint on the *model*, not on the hook's *orchestrating command*. A
  devhook's script may still perform a git action itself — e.g. creating a commit with an
  LLM-drafted message — but only after explicitly prompting the developer for confirmation first.
  The model never decides to run git on its own; a human approves, and the script (not the model)
  runs the git command.
- Any call to a hosted/agentic model (today: Claude, via `sbx`) runs inside a dedicated sandbox,
  never directly on the host. Whether this applies to a local model like Ollama is open: the
  concern is really about bounding a *tool-using* agent, so a pure text-completion call with no
  file/tool access may not need it, while giving a local model shell or file tools plausibly would.

## Hooks

- **`docstring_check/`** — post-commit check for Python docstrings left stale by the commit just
  made. Originally built and validated as a personal untracked hook in `scpca-portal`; see
  `docstring_check/requirements.md` for the full design history and decision log.

More planned: a TODO collector, a commit-message generator, a PR-comment writer.
