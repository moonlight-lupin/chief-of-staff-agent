# Running Chief of Staff in Claude Code on the web

Claude Code on the web (claude.ai/code, the Claude mobile and desktop apps)
runs each session in an **ephemeral cloud container**. The repository is
cloned fresh at start, and the container is thrown away at the end. Three
things follow from that, and this repo handles all three:

| Problem | What handles it |
|---|---|
| No `.venv`, no `company.yaml` in a fresh clone | `.claude/hooks/session-start.sh` builds the venv and links config from your data repo |
| Anything under `project_root` is lost at teardown | Your data lives in a **separate private git repo**; `chief_of_staff.py sync push` commits and pushes it |
| No secrets store, and environment variables are stored in plain text | Credential-holding providers are refused; use `provider: agent`, where Claude's own Gmail/Calendar/Drive connectors do the I/O |

## Why a separate repo

The plugin repository is public. Your pipeline, invoices, contacts, wiki, and
approval queue (`state.db`) must never be committed to it. `sync` refuses to
run if `project_root` is inside the plugin checkout, or if the data repo's
remote *is* the plugin repo.

## One-time setup

1. **Create an empty private repo on GitHub**, e.g. `you/chief-of-staff-data`.
   Leave it without a README, since the first sync creates its contents.
2. **Add it to your Claude Code environment** alongside this repo, so the
   session's git proxy can push to it. In the Claude app, open the
   environment settings and add the repository. Adding it to a single
   session also works.
3. **Set one environment variable** in the environment settings:

   ```
   CHIEF_OF_STAFF_DATA_REPO=you/chief-of-staff-data
   ```

   This is not a secret, so it is safe as a plain environment variable. If
   the environment already clones the data repo next to this one as
   `chief-of-staff-data/`, you can skip this step, because the hook finds it
   on its own.
4. **Start a session and bootstrap once.** Ask Claude:

   > Bootstrap Chief of Staff for **\<company\>**, jurisdiction **\<SG\>**,
   > operator **\<me@company.com\>**, using the agent provider and **git
   > storage** in the data repo. Then run `capabilities` and `sync push`.

   Claude will run the equivalent of:

   ```bash
   printf 'integrations:\n  workspace:\n    provider: agent\n' > /tmp/agent.yaml
   .venv/bin/python shared/scripts/bootstrap.py --company "<company>" --jurisdiction SG \
       --operator me@company.com --project-root "$CHIEF_OF_STAFF_DATA_DIR" --config /tmp/agent.yaml \
       --storage git
   .venv/bin/python shared/scripts/chief_of_staff.py capabilities --summary
   .venv/bin/python shared/scripts/chief_of_staff.py sync push
   ```

   `--storage git` records `storage.mode: git` in `company.yaml`. The data
   repo is already cloned by the hook here, so bootstrap keeps it as it is.
   `company.yaml` is written through the `shared/config/company.yaml` symlink
   into `<data repo>/config/company.yaml`, so your config is saved along with
   your data.

## Every session after that

The SessionStart hook runs before Claude does anything:

1. It installs `requirements.txt` into `.venv` (plus `pytest` and `ruff`).
2. It clones the data repo, or fast-forwards it if already cloned.
3. It links `shared/config/{company,drive-map,queries}.yaml` into
   `<data>/config/`. A real file that is already there is never replaced.
4. It exports `CHIEF_OF_STAFF_PROJECT_ROOT` for the session.

When Claude tries to finish, the Stop hook checks the data repo. If anything
is uncommitted or unpushed, Claude is told to run `sync push` before stopping.

```bash
.venv/bin/python shared/scripts/chief_of_staff.py sync status --summary
.venv/bin/python shared/scripts/chief_of_staff.py sync pull      # fast-forward only
.venv/bin/python shared/scripts/chief_of_staff.py sync push      # checkpoint, commit, push
```

## What sync guarantees

- **Never into the plugin repo.** A `project_root` inside the plugin checkout,
  or a data remote that points at the plugin repo, is refused.
- **Never secrets.** `.env` and `.env.*` are always gitignored. A data repo
  that already tracks one is refused until you remove it and rotate the
  credentials.
- **A consistent `state.db`.** The SQLite WAL is checkpointed before commit.
  The `-wal`/`-shm` sidecars and the `.runs/` operational logs are never
  committed.
- **No merges of binary state.** Pull is fast-forward only. A dirty or
  diverged data repo is refused with an explanation, and nothing is merged
  automatically.
- **Open loops are visible.** `push` warns if any review-queue action is still
  `executing`. Close it with `review_queue.py record-execution` before the
  session ends.

## What still does not work in the cloud

- `google_api`, `m365`, and `composio` providers are refused by design. Use a
  local machine or a Remote Control session for those.
- Scheduled workflow runs (`workflows start`) are refused in hosted sessions.
  Nothing runs between sessions on an ephemeral VM, so use a Routine that
  opens a new session instead.
- Two sessions writing to the same data repo at once will diverge. `sync pull`
  and `sync push` refuse rather than merge, so run one session at a time.

## Git storage is optional

Onboarding asks where your data lives, and **local files are the default**:

| Choice | How to pick it | What happens |
|---|---|---|
| `local` | `bootstrap.py --storage local`, or answer "no" in `onboard.py` | Plain files under `project_root`. `sync pull`/`push` refuse to run. |
| `git` | `bootstrap.py --storage git [--data-repo owner/name]`, or answer "yes" in `onboard.py` | `project_root` becomes a clone of your private data repo. With no `--data-repo`, a local repo is created and you add a remote later. |
| not chosen | older installs, or bootstrap without `--storage` | Nothing changes. `sync` works if `project_root` happens to be a clone. |

Bootstrap refuses git storage when `project_root` is inside the plugin
checkout, and it never clones over existing files. On a local machine you
can use either mode. Both hooks exit immediately unless
`CLAUDE_CODE_REMOTE=true`.
