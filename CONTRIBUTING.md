# Contributing

Thanks for looking at Chief of Staff. This document covers what the project
expects from a change, which is a little stricter than average because the
plugin can send email and modify a real workspace on the operator's behalf.

Security issues go through [`SECURITY.md`](SECURITY.md), not the public tracker.

## Getting set up

```bash
git clone https://github.com/moonlight-lupin/chief-of-staff-agent.git
cd chief-of-staff-agent
python -m pip install -r requirements.txt
python -m pip install pytest ruff

python shared/scripts/chief_of_staff.py demo    # no credentials needed
python -m pytest -q
```

Python 3.11 or 3.12. If the suite fails on import with
`pyo3_runtime.PanicException` from `cryptography`, you have a distro-managed
`cryptography` shadowing the pip one — `chief_of_staff.py doctor` will now name
this explicitly.

## The rules that matter

**1. The safety model is not negotiable.** Observe → Understand → Suggest →
Approve → Execute → Audit. A change may not collapse those steps. Concretely:

- Every workspace mutation goes through `workspace_guardrails.confirm_action`,
  which is **default-deny** — an action ID that is in neither `READ_ACTIONS`
  nor `WRITE_ACTIONS` is blocked. Adding a capability means adding it to the
  right set deliberately, with a test.
- An action may only reach `executed` from `executing`, and `executing` only
  from `approved`. Do not add a shortcut.
- Every mutation attempt produces an audit record, including blocked and failed
  ones.

**2. Reversibility is a design rule.** Prefer operations with a real undo path.
If a provider cannot reverse something, report the capability as `False` with a
reason rather than asserting reversibility you have not verified —
`workspace_capabilities.py` has the pattern.

**3. Never widen what a credential can reach** without saying so in the PR.

**4. Logs stay safe to share.** Tokens, secrets, message bodies, and document
contents must not reach operational logs at any level. `runtime_log.py` redacts
at write time; do not route around it.

## Tests

The project works test-first, and the commit history shows it: a `test:` commit
introducing failing contract tests, then a `fix:`/`feat:` commit making them
pass. Follow that where you can.

- Tests live in `tests/`, named `test_<topic>.py` or `test_<topic>_v<version>.py`
- `conftest.py` provides `tmp_project_dir` and `sample_company_yaml`
- **Never write to the real `skills/` tree from a test** — an autouse fixture
  sandboxes bootstrap for exactly this reason
- Anything touching state, approval, or execution needs a test for the
  *refusal* path, not only the happy path

Run the full suite before opening a PR. It should be entirely green; there are
no accepted failures.

## Style

- `ruff check shared/ skills/ hooks.py` must pass the error classes
  (`E9,F63,F7,F82`) with no findings
- Match the surrounding code — comment density, naming, and CLI conventions
- CLIs print JSON by default and human-readable output under `--summary`
- Keep skills' `SKILL.md` frontmatter to `name`, `description`, `version`,
  `license`, and `metadata`

## Pull requests

Say what changed, why, and what you ran. If the change touches the guardrail,
the pending-action state machine, the audit chain, or a provider's write path,
say so explicitly at the top of the description — those get a closer read.

Update `CHANGELOG.md` in the same PR. The changelog going stale is a recurring
failure mode here; an entry alongside the change is how it stays honest.
