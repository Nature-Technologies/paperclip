# TRC staging deploy — paperclip

Deploys this fork's image plus its private Postgres to the TRC staging host as
one compose project, `trc-staging-paperclip`. `trc-hermes-agent` and
`trc-open-webui` deploy themselves the same way; all three attach to the
shared external `poc-net` bridge. Paperclip and Postgres deliberately stay
**off** `trc-shared` — only the chat services reach the trc-backend stack, so
unlike its two siblings `trc-shared` never appears anywhere in this repo's
deploy workflow.

Bringing Paperclip up is deferrable: it is not required to unblock chat.

**Build and deploy are one workflow, one dispatch.** There is no separate
publish step any more — `trc-publish.yml` is gone. The workflow builds the
image on the runner, pushes it to GHCR, and then rolls it out to the staging
host in the same run, so the tag the deploy pulls always exists by the time it
pulls it.

The workflow runs on a **self-hosted runner** (`runs-on: [self-hosted,
linux]` — labelled, not bare `self-hosted`, so a non-Linux runner registered
later on this label set cannot pick up a job that needs docker/buildx and an
OpenSSH client), because the staging host is internal and unreachable from a
GitHub-hosted runner. The build step runs with no special Docker
configuration — it builds and pushes using the runner's own local daemon.
Only the three later steps that actually need to reach the staging host —
`Verify host preconditions`, `Pull and deploy`, `Smoke test` — set
`DOCKER_HOST: ssh://…` in their own **step-level** `env:`. That scoping is
deliberate and never a workflow-level or job-level `env:`, and never a
`docker context`:

- A `docker context` is *persistent state* on a self-hosted runner. `docker
  context create` fails with "already exists" on the second run against the
  same runner, and `docker context use` repoints that runner's **default**
  daemon for every later job that lands on it — including unrelated jobs from
  other workflows.
- `docker buildx` binds to whichever daemon is *current*. An active context
  (or a job-level `DOCKER_HOST`) would silently build the image **on the
  deploy host** instead of the runner.
- A step-level `env: DOCKER_HOST: …` cannot leak into the build step, because
  it is set only on the steps that declare it.

The build additionally needs a **pnpm lockfile refresh** before it can run:
the Docker build's `deps` stage installs with a frozen lockfile, so a
`pnpm-lock.yaml` that has drifted from `package.json` fails the build rather
than the app. `Setup pnpm` (`pnpm/action-setup@v6`, pinned to `9.15.4`,
`run_install: false`) and `Setup Node.js` (`actions/setup-node@v7`, node 20)
run before `Refresh lockfile for Docker build context`
(`pnpm install --lockfile-only --ignore-scripts --no-frozen-lockfile`), which
in turn runs before `Set up Buildx`. The `Build and push` step itself pins
`target: production` explicitly — the Dockerfile declares a later `cloud`
stage, and omitting `target:` would silently publish that stage instead. This
deploy only ever consumes the `production` target.

Three consequences of the remote-daemon design worth knowing:

- The rendered `.env.staging` file is read by the **local** Compose CLI via
  `--env-file` and is never copied to the server. Its values reach the
  containers as container environment, sent over the Docker API through the
  SSH tunnel — there is no plaintext secret file on the staging host.
- This project has **no config bind mount** to copy — unlike `trc-hermes-agent`'s
  `config.yaml`, nothing needs to be `scp`'d onto the host for compose to
  start. But `/srv/trc/staging/paperclip-home` **is** a pre-existing bind
  mount this deploy does not manage the way it manages the Postgres volume: a
  `DOCKER_HOST` proxies the Docker API, not a shell, so it cannot stat a path
  on the host filesystem. The guards on that directory (see below) therefore
  stay on plain SSH, in the `Verify the paperclip-home guards` step, and run
  right after the SSH key is loaded — **before GHCR login and before the
  build even starts**, so a typo'd path fails in seconds rather than after a
  full build.
- `docker compose pull` sends the **runner's** registry credentials to the
  remote daemon (`X-Registry-Auth`), not the host's — the staging host never
  **stores** a credential of its own. The workflow logs in to `ghcr.io` on the
  runner with the built-in `GITHUB_TOKEN` before pulling; this works whether
  the package is public (as it is today) or private.

**The runner is persistent and SHARED.** Unlike a GitHub-hosted runner, this
machine is reused by later jobs — including `trc-hermes-agent` and
`trc-open-webui`'s deploys, which can run **concurrently** with this one on
the same `$HOME`. **Three** files in that `$HOME` would otherwise be shared,
and they carry different risk. Two are now isolated per job; only
`known_hosts` remains genuinely shared:

- **The private key is a hard collision, so it is structurally isolated.** It
  is written to `$RUNNER_TEMP/id_rsa` — **never** `~/.ssh/id_rsa` — and loaded
  into a per-job `ssh-agent` whose socket is exported via `$GITHUB_ENV` for
  every later step in that job. A shared `~/.ssh/id_rsa` would let one job's
  cleanup (`rm -f ~/.ssh/id_rsa`) delete the key a sibling job is mid-deploy
  with; per-job `$RUNNER_TEMP` isolation makes that impossible, since
  `$RUNNER_TEMP` is scoped to the individual job. The agent authenticates both
  the explicit `ssh`/`scp` calls (which also pass `-i "$RUNNER_TEMP/id_rsa"`
  explicitly, redundantly with the agent, since that costs nothing) and the
  `ssh` the Docker CLI spawns internally for `DOCKER_HOST`. The key file and
  the agent are both removed in the final `Clean up secrets on the runner`
  step, which runs with `if: always()` — including the path where an earlier
  step failed before the agent ever started.
- **`~/.docker/config.json` is a hard collision too, and is likewise
  structurally isolated.** The deploy job sets `DOCKER_CONFIG` as **job-level**
  `env:`, pointing at a per-job directory outside the build context, and creates
  it before `docker login`. Every `docker`, `docker compose`, `docker buildx`
  and `docker/login-action` call in the job honours that variable, so the GHCR
  credential and buildx's state both live in job-private scratch space. Without
  it the final cleanup step's `docker logout ghcr.io` would strip the GHCR
  credential out from under a sibling repo's concurrent `docker compose pull` —
  which is exactly why the logout is safe to keep now: it can only touch this
  job's own directory, which the cleanup then deletes outright.

  Note that job-level here is correct and is **not** the hazard a job-level
  `DOCKER_HOST` would be. `DOCKER_CONFIG` says only *where credentials live*,
  never *which daemon to talk to*, so unlike `DOCKER_HOST` it cannot redirect
  the build step off the runner. (It is built from `github.workspace` rather
  than the more obvious `runner.temp` because the `runner` context is not
  available in `jobs.<job_id>.env` — GitHub rejects the whole workflow with
  "Unrecognized named-value: 'runner'".)
- **`~/.ssh/known_hosts` is still genuinely shared, and that is left as a
  documented operational requirement rather than fixed structurally** — the
  risk is lower (worst case under a race is a redundant rescan, not a hard
  auth failure) and there is no per-job equivalent of `$RUNNER_TEMP` for a
  file every job needs to read. The host-key scan writes into `$RUNNER_TEMP`
  first, removes any stale entry for *this* host with `ssh-keygen -R` (both
  the bare-host and `[host]:port` spellings), and only then **appends**
  (`>>`) to the real file — never a bare `>` or a `tee` without `-a`, either
  of which would truncate entries other jobs rely on. **This means the runner
  that executes this workflow must process one job at a time**, and if more
  than one self-hosted runner is registered on the same machine (e.g. to
  parallelize trc-hermes-agent, trc-open-webui and paperclip deploys), each
  runner must run as a **separate OS user** so they do not share `$HOME` and
  therefore do not share `~/.ssh/known_hosts`.
- `.env.staging` is likewise removed in the final cleanup step.

## Host prerequisites

### One-time host preparation (before the FIRST dispatch)

`bootstrap: true` cannot stand up a host on its own from nothing, and the list
below is what the workflow genuinely cannot do for itself. Run this **once per
host, as root or with sudo, before any dispatch** — including a `bootstrap:
true` one:

```
sudo install -d -o <deploy-user> -g <deploy-user> /srv/trc /srv/trc/staging
```

Both levels are needed, not just the leaf: the deploy's host-prep step runs
`mkdir -p` under `/srv/trc/staging`, which needs write on that directory, and
creating `/srv/trc/staging` itself needs write on `/srv` — neither of which a
non-root deploy user has on a fresh host. The deploy never uses `sudo`, by
design, so this cannot be folded into the workflow.

**`bootstrap: true` needs one more thing in this repo specifically.** Its
`paperclip-home` creation path runs `chown 1000:1000` over SSH, and `chown` to
another UID requires **root** — a plain deploy user cannot do it. So
`bootstrap: true` can only create `paperclip-home` if **either**:

- the deploy user **is itself UID 1000** (then `mkdir` + `chown 1000:1000` is a
  no-op chown to its own uid, which is permitted), **or**
- that one `chown` is covered by passwordless sudo for the deploy user.

If neither holds, `paperclip-home` must be **pre-created by hand**, and then
`bootstrap: true` will find it already correct and skip the creation path
(the existence and `stat -Lc '%u'` guards still run, unconditionally):

```
sudo install -d -o 1000 -g 1000 /srv/trc/staging/paperclip-home
```

The deploy creates **only** `poc-net`, idempotently, on every run. Outside
`bootstrap` mode (see below) it creates neither `trc-staging-paperclip-db` nor
`/srv/trc/staging/paperclip-home`, and it fails if either is missing or wrong:

- **`trc-staging-paperclip-db` must already exist and already hold Paperclip's
  Postgres data**, unless this is a genuinely fresh host. The `Verify host
  preconditions` step does not run `docker volume create` on a routine
  deploy: the volume is declared `external: true` precisely so Compose
  refuses to start without it, and pre-creating it would boot Postgres
  against a silently *empty* volume — a brand-new database, Paperclip with no
  data — with every smoke test below still passing.
- **`/srv/trc/staging/paperclip-home` must already exist and be owned by UID
  1000**, unless this is a genuinely fresh host. See the section on it below.
  No `trc-shared` check exists here at all — paperclip and postgres are
  `poc-net`-only by design.

### The `bootstrap` input

`workflow_dispatch` takes a `bootstrap` boolean input, default `false`. It
does **double duty**, gating two independent things at once:

- In the `Verify host preconditions` step (`DOCKER_HOST`), when `true` it
  **creates** `trc-staging-paperclip-db` instead of failing when it is
  missing, and logs an `::warning::`.
- In the `Verify the paperclip-home guards` step (plain SSH), when `true` it
  **creates** `/srv/trc/staging/paperclip-home`, owned by UID 1000, if it is
  missing, and **permits an empty directory** to pass the instance-state
  check that otherwise rejects one. Both are logged loudly.

Use `bootstrap: true` **only** when standing up a genuinely fresh host in one
dispatch, or for the **first deploy of a fresh Paperclip instance** (see
"First-run bootstrap" below) — it lets that first deploy succeed without a
human pre-creating the volume and directory by hand over SSH. A volume or
directory created this way is empty; it is not a substitute for migrating
real data on a host that is supposed to already have it.

Leave `bootstrap` at its default `false` on every routine deploy. With it
`false`, both the volume check and all three paperclip-home guards stay
fail-closed exactly as before.

The deploy user (`USERNAME`) needs:

- **read/write access to `/srv/trc/staging/paperclip-home`** (for the guard
  checks, and for `bootstrap: true` to create and `chown` it) and read access
  to `/srv/trc/staging/fingerprints` (to read hermes-agent's published
  fingerprint during the smoke test);
- **membership of the `docker` group** (or root) on the host, so the SSH
  session backing `DOCKER_HOST` can reach the daemon socket without `sudo`.

### Runner prerequisites

The runner this workflow executes on must **process one job at a time** —
`~/.ssh/known_hosts` is genuinely shared across jobs (see "The runner is
persistent and SHARED" above), and the non-destructive scan-then-append
pattern assumes no other job is touching that file concurrently. If more than
one self-hosted runner is registered on the same machine — e.g. to let
trc-hermes-agent, trc-open-webui and paperclip deploy in parallel — each
runner **must run as a separate OS user**, so they do not share `$HOME` and
therefore do not share `~/.ssh/known_hosts`. The private key itself does not
have this constraint: it lives under the job-scoped `$RUNNER_TEMP`, so two
jobs on the same runner user cannot collide over it even if this requirement
is violated. Neither does `~/.docker/config.json`, which the job-level
`DOCKER_CONFIG` moves into a per-job directory. **`known_hosts` is the only
genuinely shared file left**, which is why this requirement is about that file
specifically.

### Phase 2 preconditions

**Paperclip starts fresh.** Unlike hermes-agent and open-webui, it needs no
data migrated, so Phase 2 for this repo is only:

0. The one-time `sudo install -d … /srv/trc /srv/trc/staging` from "One-time
   host preparation" above. This one is **never** optional — nothing in the
   workflow can create it.
1. `docker volume create trc-staging-paperclip-db` — empty is correct here.
2. `sudo install -d -o 1000 -g 1000 /srv/trc/staging/paperclip-home` — empty is
   correct here too.
3. Dispatch the deploy **once** with `bootstrap: true`.

Step 1 **is** skippable — `bootstrap: true` creates the volume itself, over the
Docker API, needing no host privileges. Step 2 is skippable **only if the
deploy user is UID 1000 or has passwordless sudo for that `chown`**; see
"One-time host preparation" above for why. With an ordinary non-1000 deploy
user, `bootstrap: true` fails on the `chown` and step 2 must be run by hand.
Step 0 is never skippable under any configuration.

## Running a deploy

The workflow builds and publishes two tags on every dispatch, but they play
different roles:

- `ghcr.io/nature-technologies/trc-paperclip:staging` — a **moving** pointer,
  overwritten by every run. Pushed for human convenience (e.g. browsing the
  GHCR package), but **nothing in this workflow ever deploys it.**
- `ghcr.io/nature-technologies/trc-paperclip:git-<7-char-sha>` — an
  **immutable** tag naming the exact commit that was built. **This is what
  gets deployed.** The `Render the environment file` step sets
  `DEPLOY_IMAGE: ${{ steps.tags.outputs.sha }}` in its own `env:` and writes
  that value into `.env.staging` as `PAPERCLIP_IMAGE`, so the deploy runs
  exactly what this run built — "what is running" is unambiguous, and never
  depends on `:staging` having been overwritten by a later, unrelated run
  between build and deploy.

1. Actions → **TRC staging deploy (paperclip)** → Run workflow.
2. Leave `bootstrap` unchecked (default `false`) unless this is the first
   deploy to a brand-new host or a deliberate first deploy of a fresh
   instance (see "First-run bootstrap" below).
3. The workflow builds the image from the checked-out ref (`target:
   production`, after refreshing the pnpm lockfile), pushes both tags, and
   deploys the `:git-<7-char-sha>` one it just pushed.

The `Pull and deploy` step logs the deployed tag by reading it back out of
`.env.staging` (`grep '^PAPERCLIP_IMAGE=' .env.staging`) rather than
recomputing it, so the log line can never drift from what is actually
running — including under the rollback override below.

### Rolling back

**There is no digest input any more.** Because every routine deploy already
runs the immutable tag it just built, rolling back to an *older* build means
re-dispatching the workflow from a branch where the **`Render the environment
file`** step — not `Pull and deploy` — has its `DEPLOY_IMAGE` env line
hardcoded to an older tag instead of the dynamic
`${{ steps.tags.outputs.sha }}` expression:

1. Branch off the current `dev` (name it anything that is not `dev`, e.g.
   `rollback/2026-07-27`).
2. In `.github/workflows/trc-staging-deploy.yml` on that branch, find the
   `Render the environment file` step and change its
   `DEPLOY_IMAGE: ${{ steps.tags.outputs.sha }}` line to a hardcoded
   `DEPLOY_IMAGE: ghcr.io/nature-technologies/trc-paperclip:git-<short-sha>`
   — pick the short sha from a previous run's logs or the GHCR package's tag
   list. Commit and push the branch.
3. Actions → **TRC staging deploy (paperclip)** → Run workflow, and select
   **that branch** as the workflow ref (the "Use workflow from" selector). The
   deploy is `workflow_dispatch`-only, so it runs the workflow definition from
   whichever ref you pick.
4. Delete the branch once you are done. To roll forward again, dispatch the
   deploy from `dev` as normal, which builds fresh and deploys its own new
   `:git-<sha>`.

Because build and deploy are unified, a rollback dispatch **still rebuilds and
pushes fresh `:staging`/`:git-<newsha>` tags from that branch's source** — but
with `DEPLOY_IMAGE` hardcoded as above, the deploy step itself pulls and runs
the specific **older** tag you named, not the one it just built. A plain
re-dispatch from an old branch, without that edit, is not itself a rollback —
it would build and deploy a fresh image from old source under a new sha.

The `Pull and deploy` step still prints the digest that actually landed, so
every run log records what is now running.

## Required repository secrets (environment: `staging`)

The environment name is lowercase `staging` — GitHub Actions matches
environment names case-sensitively, so a `Staging` environment's secrets will
not resolve here.

| Secret | Notes |
|---|---|
| `SSH_PRIVATE_KEY_DEV` | Deploy user's private key |
| `HOST` | Staging host, used both for `ssh-keyscan`, the paperclip-home guard's plain `ssh`, the fingerprint read, and every step's `DOCKER_HOST: ssh://${USERNAME}@${HOST}:${SSH_PORT}` |
| `USERNAME` | Deploy user on the host |
| `SSH_PORT` | Optional, defaults to 22. Threaded through every consumer that needs it: the `ssh-keyscan` that seeds `known_hosts`, the paperclip-home guard's `ssh` call, every step's `DOCKER_HOST`, and the smoke test's fingerprint read over `ssh` — a mismatch between any of these would scan or dial a different endpoint than the others |
| `POSTGRES_PASSWORD` | Also interpolated into `DATABASE_URL`. The workflow rejects an empty value or one containing `@ : / ? #`, since those characters silently corrupt the connection string with no error from compose |
| `BETTER_AUTH_SECRET`, `PAPERCLIP_TOOL_ACTION_SIGNING_SECRET` | `openssl rand -hex 32` |
| `OPENROUTER_API_KEY` | |
| `PAPERCLIP_PUBLIC_URL` | The external URL a browser can reach. The workflow rejects `localhost`/`127.0.0.1` — this value is baked into auth callbacks and shown to the first admin |

Host keys are **scanned at deploy time**
(`ssh-keyscan -T 10 -p "$SSH_PORT" -H "$HOST" > "$RUNNER_TEMP/known_hosts"`)
rather than pinned in advance, and then merged into `~/.ssh/known_hosts`
non-destructively — see "The runner is persistent and SHARED" above for why
it is never a bare `>` (or an unadorned `tee`) onto the real file.
Trust-on-first-use has the same trade-off it always did:

- It still protects against a passive attacker who cannot intercept the very
  first connection of a run — `StrictHostKeyChecking` is never disabled, so if
  the host key changes *after* the scan (e.g. mid-run, or on a subsequent run
  against a key that was swapped since the last scan) the connection still
  aborts rather than silently trusting a new key.
- It does **not** protect against an active machine-in-the-middle present at
  the moment `ssh-keyscan` runs, since there is no prior pinned key to compare
  against. This is a deliberate trade against the operational cost of
  maintaining a pinned-key secret in step with any host-key rotation.

Every secret rendered into `.env.staging` is charset-guarded: the workflow
rejects any value containing `$`, a backtick or `#`. Compose's `env_file` parser
interpolates the first two and treats `#` as a comment, so such a value would
reach the container as a *different* string with nothing erroring — a mangled
`BETTER_AUTH_SECRET` looks like auth cookies that never validate, a mangled
`PAPERCLIP_TOOL_ACTION_SIGNING_SECRET` looks like tool actions rejected as
unsigned. `openssl rand -hex 32` never produces any of them. This guard
applies to `POSTGRES_PASSWORD` too — Compose interpolates a `$` in that value
exactly the same as any other, and it is additionally interpolated into
`DATABASE_URL`, where `@ : / ?` also corrupt the connection string, so
`POSTGRES_PASSWORD` carries **both** guards. `PAPERCLIP_PUBLIC_URL` is checked
for `$`/backtick/`#` only — it legitimately contains `:` and `/`, so the
stricter `POSTGRES_PASSWORD` pattern must not be applied to it. `.env.staging`
itself is written on the runner and passed to Compose with `--env-file`; it
is never copied to the host, and it is deleted by the final cleanup step even
when an earlier step in the run fails.

The Postgres smoke check runs a real authenticated `select 1` rather than
`pg_isready`, which never authenticates and would return success against a
password it had never seen. It connects with `-h postgres` (the compose
service alias over `poc-net`), never the unix socket or `127.0.0.1`: this
image's generated `pg_hba.conf` grants `trust` on both of those, so a wrong
password would return exit 0 and the check would prove nothing. The password
is passed via `-e PGPASSWORD` with no `=value`, so it is taken from the step's
own shell environment and never enters the `docker` CLI's argv. This call is
not heredoc-fed, so it correctly carries no `-i`.

## `/srv/trc/staging/paperclip-home` — read this before deploying

The bind mount at `/paperclip` holds `instances/default/`: `workspaces/`,
`data/run-logs/`, `data/backups/`, `logs/`, `config.json`, and
`secrets/master.key`.

**Nothing is migrated into it. Paperclip starts fully fresh in Phase 2** — do
**not** copy the old Postgres volume, and do **not** copy the old
`./paperclip-data` directory from the retired stack. Postgres ignores
`POSTGRES_PASSWORD` on an already-initialised data directory, so a copied
volume would leave the freshly generated secret unable to authenticate
against it; a fresh cluster initialises with the new secret instead, which is
why `POSTGRES_PASSWORD` stays a rotatable repository secret and no `ALTER
USER` step is needed.

Three guards run in the `Verify the paperclip-home guards` step — over plain
SSH, and *before anything else* in the workflow that touches the host or
renders a secret (before GHCR login, before the build, before `Verify host
preconditions`), so a wrong path or owner cannot leave a rendered
`.env.staging` half-applied and fails in seconds rather than after a full
build:

- **The directory is created only under `bootstrap: true`.** Outside
  bootstrap the deploy will never create it: it holds `secrets/master.key`
  and the only copy of the `hermes_gateway` wiring, so a typo'd path must
  fail loudly rather than be silently accepted as a fresh install. Under
  `bootstrap: true`, a missing directory is created and `chown`'d to UID
  1000, loudly (`::warning::`). The workflow fails if it is absent and
  `bootstrap` is `false`, and the validator fails if anyone adds an
  unconditional `mkdir` for it — by literal path or via
  `$PAPERCLIP_HOME_DIR`/`${PAPERCLIP_HOME_DIR}` — reachable outside a branch
  gated on the `bootstrap` input. Never substitute a bind-mounted probe
  container for this check either: Docker creates a missing bind source as
  root, which is exactly the failure the guard exists to prevent.
- **It must be owned by UID 1000.** Bind mounts carry host ownership straight
  through and the container runs as `node`, so without that ownership the
  first write to `/paperclip` fails with `EACCES` and the container
  crash-loops. The workflow checks the owner with `stat -Lc '%u'` (the `-L`
  dereferences symlinks, since both `[ -d ]` and Docker's bind mount follow
  them) and refuses to deploy otherwise. **This check is unconditional** —
  `bootstrap` never relaxes it, and it still runs even immediately after a
  bootstrap-mode creation, since the creation's own `chown` is not treated as
  a substitute for verifying it.
- **It must hold instance state — unless `bootstrap: true`.** Normally a
  present-but-empty directory would defeat the first guard just as
  effectively as a missing one, so the workflow requires at least one of
  `instances/default/config.json` or `secrets/master.key`. On a deliberate
  fresh start the directory *is* legitimately empty, so this one check — and
  only this one — is relaxed by the `bootstrap` input. **Needed for the first
  deploy only**; leave it `false` afterwards, or an accidentally emptied
  `paperclip-home` stops being caught.

`config.json` carries the `hermes_gateway` adapter's URL and API key — there
is no `HERMES_*` environment variable in the compose file, and that is not an
omission. It is read-only after the instance is claimed, so the deploy can
only fingerprint-check that key against `trc-hermes-agent`'s copy, not set
it.

## First-run bootstrap

There is no CLI invite flow. For `authenticated`/`private` mode the first
admin opens `PAPERCLIP_PUBLIC_URL` in a browser, signs in or creates an
account, and chooses **Claim this instance**. Starting fresh means the old
`instances/default/config.json` is not being carried over, so its
`hermes_gateway` URL and API key have to be **re-entered by hand** after
claiming: use `http://hermes-agent:8642` with the same `HERMES_API_KEY` value
the other two repos deploy. The smoke test's warn-only fingerprint check (see
below) compares it against `trc-hermes-agent`'s copy and warns on a mismatch,
but it cannot set the value — that file is read-only after claiming.

Dispatch that first deploy with `bootstrap: true`. It both permits the
empty-but-present (or entirely missing) `paperclip-home` and creates
`trc-staging-paperclip-db` if it does not already exist.

## Checks

`deploy/trc/validate_compose.py` asserts the invariants that make three
independent compose projects add up to one stack — `external: true` on every
network and volume, `trc-shared` staying absent, Postgres publishing no ports,
and the `depends_on: postgres` ordering being kept. Each **service's** own
`networks:` membership is asserted too, not just the top-level keys, since a
top-level network no service joins is silently ignored and an extra one added
to a service would quietly put it on the backend bridge. For the deploy
workflow specifically it also asserts:

- the job requests the **`self-hosted`** runner label (all three `runs-on`
  spellings understood: scalar, list, and the `{group, labels}` mapping) —
  `ubuntu-latest` cannot reach the internal staging host at all;
- `environment` is exactly **`staging`**, lowercase — GitHub matches
  environment names case-sensitively, so `Staging` resolves no secrets and
  every one of them arrives as the empty string;
- **every** `DOCKER_HOST` value references both `secrets.HOST` and
  `secrets.USERNAME`, so a literal host cannot be substituted. Of the three
  assertions above this is the one that catches a **silent** failure: a
  hardcoded `ssh://root@10.0.0.9:22` renders every application secret and
  deploys them to whatever machine that literal names, with the smoke tests
  passing against it. The other two fail loudly at runtime;
- the `Write SSH key and scan the host key` step **positively** contains the
  whole non-destructive `known_hosts` merge: an `ssh-keyscan` into
  `$RUNNER_TEMP`, a `test -s` on it, `ssh-keygen -R` **twice** (the bare-host
  and `[host]:port` spellings), and an append (`>>`) onto
  `~/.ssh/known_hosts`. The "never truncate" rule below is negative-only, and
  on its own it passes a workflow that has no host-key handling at all;
- the `if: always()` cleanup step runs `ssh-agent -k` — deleting the key file
  does not unload the key, and a leaked agent keeps it decrypted in memory on
  this persistent runner, one more per dispatch;
- `docker context create` appears **nowhere** in the workflow;
- `DOCKER_HOST` appears only as **step-level** `env:`, never at workflow or
  job level — either would apply to the build step too;
- `DOCKER_HOST` is present, specifically, on each of the `Verify host
  preconditions`, `Pull and deploy` and `Smoke test` steps by name — not just
  "at least one step has it" (which would let it silently go missing from any
  one of the three while the others stay green);
- the step running `docker/build-push-action` has no `DOCKER_HOST` in its own
  `env:` and pins `target: production` — it must build on the runner, not the
  deploy host, and must never silently publish the later `cloud` stage;
- the `Verify the paperclip-home guards` step has no `DOCKER_HOST` in its own
  `env:` and reads ownership with `stat -Lc` — these checks stat a path on
  the host filesystem, which a `DOCKER_HOST` (an API proxy, not a shell)
  cannot do;
- `bootstrap` is declared as a `workflow_dispatch` input, typed `boolean`,
  defaulting to `false`;
- no line writes raw `ssh-keyscan` output directly onto `known_hosts` outside
  `$RUNNER_TEMP`, whether via a bare `>` or piped through `tee`;
- the private key is never written to `~/.ssh/id_rsa` anywhere in the
  workflow — only under `$RUNNER_TEMP`;
- an `if: always()` cleanup step exists that removes `id_rsa` and
  `.env.staging` and runs `docker logout` (which is now confined to this job's
  own `DOCKER_CONFIG` directory, and cannot strip a sibling job's credential);
- `docker volume create` and any `mkdir` of the paperclip-home bind-mount
  source (by literal path, `$PAPERCLIP_HOME_DIR`, or `${PAPERCLIP_HOME_DIR}`)
  are each only reachable from inside a branch whose **enclosing** `if`/`elif`
  condition tests the `bootstrap` input — checked with an if/elif/else/fi-aware
  scan, not merely "does `bootstrap` appear earlier in the step", so an
  unconditional create placed *after* that branch's `fi` (i.e. no longer
  actually gated by anything) is still rejected;
- Compose is invoked with `--env-file`, so secret values are never
  interpolated into a shell command string;
- `compose pull` precedes `compose up -d` (same-line `pull && up -d` counts,
  compared by position within the line), so a deploy can never silently
  redeploy an image already on the host;
- every heredoc-fed `docker exec` passes `-i`, and no `docker exec` that is
  *not* heredoc-fed passes `-i` — a heredoc without `-i` gets no stdin, so the
  step's body never runs and it still exits 0. This repo's Postgres smoke
  check (`docker exec -e PGPASSWORD paperclip-postgres psql ...`) is the
  deliberate non-heredoc case proving the rule the other way.

It runs in the **TRC deploy checks** workflow. Read its docstring before
changing the compose file or the deploy workflow.
