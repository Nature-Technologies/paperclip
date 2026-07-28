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
publish step — `trc-publish.yml` is gone.

**The build runs ON the deploy host**, via a **job-level** `DOCKER_HOST`, the
same arrangement `trc-hermes-agent` and `trc-open-webui` use. buildx's default
`docker-container` driver starts with an **empty cache on every dispatch**,
which made each deploy a cold 15–45 minute build; the host daemon's own layer
store is the only cache that persists between runs. Only the source context
crosses the network, never an image.

**There is no registry in the loop.** `push: false` puts the image straight
into the deploy host's image store, which is the same daemon `docker compose
up -d` talks to, so the tag the deploy runs always exists by the time it runs
it — there is nothing to pull. The compose service sets `pull_policy: never`
so a missing image **fails closed** instead of falling back to GHCR, where the
retired publish-then-pull scheme left tags that a rollback dispatch could
otherwise silently start. Nothing logs in to a registry, which is what makes
the old per-job `DOCKER_CONFIG` (and the `docker logout` that required it)
removable.

> **Rollback targets are host-local.** `:git-<7-char-sha>` tags exist only in
> that host's image store. A `docker image prune -a` there, or a host rebuild,
> destroys **every** rollback target. Read "Rolling back" below before pruning
> on the staging host.

The workflow runs on a **self-hosted runner** (`runs-on: self-hosted`) because
the staging host is internal and unreachable from a GitHub-hosted runner.

Because `DOCKER_HOST` is job-level, **every** `docker` call in the job — the
build included — goes to the staging host over SSH. Two consequences follow
directly:

- **`Write SSH key and scan the host key` must be the first step after
  checkout.** The `ssh` the Docker CLI spawns internally for `DOCKER_HOST` is
  authenticated by the per-job `ssh-agent` that step starts, so any `docker`
  call placed ahead of it fails host-key verification. The validator asserts
  the ordering.
- **Never a `docker context`.** A context is *persistent state* on a
  self-hosted runner: `docker context create` fails "already exists" on the
  second run, and `docker context use` repoints that runner's **default**
  daemon for every later job on the machine, including `trc-hermes-agent`'s
  and `trc-open-webui`'s. The job-level `DOCKER_HOST` is scoped to this job and
  cannot leak. A **step-level** `DOCKER_HOST` is banned too now — it would
  *override* the job-level one and silently point that single step at a
  different daemon than the build and the deploy.

The build still needs a **pnpm lockfile refresh** first, and that refresh still
runs **on the runner**: the build context is assembled here and uploaded to the
host's daemon, so the `pnpm-lock.yaml` the `deps` stage installs from is the
one this step leaves behind. The Docker build installs with a frozen lockfile,
so a lockfile that has drifted from `package.json` fails the build rather than
the app. `Setup pnpm` (`pnpm/action-setup@v6`, pinned to `9.15.4`,
`run_install: false`) and `Setup Node.js` (`actions/setup-node@v7`, node 20)
run before `Refresh lockfile for Docker build context`
(`pnpm install --lockfile-only --ignore-scripts --no-frozen-lockfile`), which
in turn runs before `Set up Buildx`. `Set up Buildx` pins `driver: docker` —
that is the whole performance argument, since it binds the build to the remote
daemon's own persistent layer store. `Build the image on the deploy server`
pins `target: production` explicitly — the Dockerfile declares a later `cloud`
stage, and omitting `target:` would silently deploy that stage instead. It sets
no `platforms` (build native to the server, or buildx switches on QEMU
emulation), no `cache-from`/`cache-to` (the daemon's own layer store *is* the
cache now, and the `docker` driver cannot use the gha backend anyway), and
`provenance: false` (attestations force a manifest list and an extra export
pass for nothing).

Three consequences of this design worth knowing:

- **No env file is written anywhere.** The `Deploy` step binds every secret as
  its own `env:`, and Compose resolves the compose file's `${…}` references
  from that process environment. No secret touches disk on the runner *or* the
  server, and nothing is dotenv-parsed — so a `$`, a backtick or a `#` inside a
  secret is taken **literally** instead of being interpolated or truncated.
  That is why the charset guard the retired `.env.staging` rendering needed is
  gone; the `@ : / ? #` guard on `POSTGRES_PASSWORD` is not, being about
  `DATABASE_URL` rather than about parsing.
- **`/srv/trc/staging/paperclip-home` is a pre-existing bind mount** this
  deploy verifies rather than manages. A `DOCKER_HOST` proxies the Docker API,
  not a shell, so it cannot stat a path on the host filesystem; the guards on
  that directory (see below) therefore reach the host over plain `ssh`, in the
  `Verify the paperclip-home guards` step, which runs **before the build even
  starts** so a typo'd path fails in seconds rather than after a full build.
  Bind-mount sources are resolved by the **remote** daemon, so the path must be
  absolute and must already exist on the server.
- **Free disk on the host is now a precondition of the build itself**, not just
  of the deploy. `Verify host preconditions` reads `df` over SSH and fails
  under 10 GB, warns under 25 GB — BuildKit's out-of-space failures name
  everything except the cause, and a full disk has to fail in seconds rather
  than 28 minutes in.

**The runner is persistent and SHARED.** Unlike a GitHub-hosted runner, this
machine is reused by later jobs — including `trc-hermes-agent` and
`trc-open-webui`'s deploys, which can run **concurrently** with this one on
the same `$HOME`. Two of the three files that used to be shared there are gone
as a category; only `known_hosts` is still genuinely shared:

- **The private key is structurally isolated.** It is written to
  `$RUNNER_TEMP/id_rsa` — **never** `~/.ssh/id_rsa` — and loaded into a per-job
  `ssh-agent` whose socket is exported via `$GITHUB_ENV` for every later step in
  that job. A shared `~/.ssh/id_rsa` would let one job delete the key a sibling
  job is mid-deploy with; `$RUNNER_TEMP` is scoped to the individual job and the
  runner clears it at the start of each one, which makes that impossible. The
  agent authenticates both the explicit `ssh` calls (which also pass
  `-i "$RUNNER_TEMP/id_rsa"` explicitly, redundantly with the agent, since that
  costs nothing) and — load-bearing under a job-level `DOCKER_HOST` — the `ssh`
  the Docker CLI spawns internally for **every** `docker` call in the job,
  including the build.
- **`~/.docker/config.json` is no longer touched at all.** Nothing logs in to a
  registry, so there is no credential to isolate, no `docker logout` that could
  strip a sibling job's credential mid-deploy, and no per-job `DOCKER_CONFIG`
  needed to contain either. The validator rejects a `docker login`,
  `docker logout` or `docker/login-action` reappearing.
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

**There is no cleanup step any more**, matching `trc-hermes-agent` and
`trc-open-webui` — and the two bullets above are what make its removal safe
rather than an oversight. No env file is rendered and nothing logs in to a
registry, so the private key is the only secret that still reaches the runner's
disk, and it lives under `$RUNNER_TEMP`, which the runner clears itself. The
validator pins that reasoning down: if any step ever names `id_rsa` outside
`$RUNNER_TEMP`, or writes an env file, the check fails and the cleanup step has
to come back with it.

**The per-job `ssh-agent` is left running, though.** It holds the *decrypted*
private key in memory on this persistent runner until the machine reboots — one
more agent per dispatch. Reap them with `pkill ssh-agent` on the runner if that
accumulation matters.

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

- In the `Verify host preconditions` step (over the Docker API), when `true` it
  **creates** `trc-staging-paperclip-db` instead of failing when it is
  missing, and logs an `::warning::`.
- In the `Verify the paperclip-home guards` step (plain `ssh`), when `true` it
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
is violated. Neither does `~/.docker/config.json`, which nothing in the
workflow touches any more. **`known_hosts` is the only genuinely shared file
left**, which is why this requirement is about that file specifically.

The runner also needs enough **free disk on the staging host**, not on itself:
the build happens there now. `Verify host preconditions` fails under 10 GB and
warns under 25 GB before the build starts.

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

The workflow tags the image twice on every dispatch, **in the deploy host's
local image store** — nothing is pushed anywhere. The two tags play different
roles:

- `ghcr.io/nature-technologies/trc-paperclip:staging` — a **moving** pointer,
  overwritten by every run. Kept for human convenience when reading `docker
  images` on the host, but **nothing in this workflow ever deploys it.**
- `ghcr.io/nature-technologies/trc-paperclip:git-<7-char-sha>` — an
  **immutable** tag naming the exact commit that was built. **This is what
  gets deployed.** The `Deploy` step sets
  `PAPERCLIP_IMAGE: ${{ steps.tags.outputs.sha }}` in its own `env:`, so the
  deploy runs exactly what this run built — "what is running" is unambiguous,
  and never depends on `:staging` having been overwritten by a later, unrelated
  run between build and deploy.

The `ghcr.io/…` spelling is kept even though nothing is pushed there: it is now
only a local tag name, and keeping the spelling means run logs and rollback
tags read the same as they did under the retired publish-then-pull scheme.

1. Actions → **TRC staging deploy (paperclip)** → Run workflow.
2. Leave `bootstrap` unchecked (default `false`) unless this is the first
   deploy to a brand-new host or a deliberate first deploy of a fresh
   instance (see "First-run bootstrap" below).
3. The workflow builds the image **on the staging host** from the checked-out
   ref (`target: production`, after refreshing the pnpm lockfile on the
   runner), tags it twice there, and deploys the `:git-<7-char-sha>` one.

The `Deploy` step echoes `$PAPERCLIP_IMAGE` — the same variable Compose
resolves — so the log line can never drift from what is actually running,
including under the rollback override below. It then records the local image
id. There is no digest to print any more: `RepoDigests` is only ever set for an
image that went through a registry.

### Rolling back

> **Read this first: rollback targets are HOST-LOCAL.** `:git-<7-char-sha>`
> tags now live only in the staging host's image store. A `docker image prune
> -a` there, or a host rebuild, destroys every one of them, and there is no
> registry copy to fall back on — `pull_policy: never` makes that fail closed
> rather than silently starting a stale GHCR image. Check `docker images` on
> the host for the tag you intend to roll back to **before** you start.

**There is no digest input.** Because every routine deploy already runs the
immutable tag it just built, rolling back to an *older* build means
re-dispatching the workflow from a branch where the **`Deploy`** step has its
`PAPERCLIP_IMAGE` env line hardcoded to an older tag instead of the dynamic
`${{ steps.tags.outputs.sha }}` expression:

1. Branch off the current `dev` (name it anything that is not `dev`, e.g.
   `rollback/2026-07-27`).
2. In `.github/workflows/trc-staging-deploy.yml` on that branch, find the
   `Deploy` step and change its `PAPERCLIP_IMAGE: ${{ steps.tags.outputs.sha }}`
   line to a hardcoded
   `PAPERCLIP_IMAGE: ghcr.io/nature-technologies/trc-paperclip:git-<short-sha>`
   — pick the short sha from a previous run's logs, and confirm it is still in
   the host's image store. Commit and push the branch.
3. Actions → **TRC staging deploy (paperclip)** → Run workflow, and select
   **that branch** as the workflow ref (the "Use workflow from" selector). The
   deploy is `workflow_dispatch`-only, so it runs the workflow definition from
   whichever ref you pick.
4. Delete the branch once you are done. To roll forward again, dispatch the
   deploy from `dev` as normal, which builds fresh and deploys its own new
   `:git-<sha>`.

Because build and deploy are unified, a rollback dispatch **still rebuilds
fresh `:staging`/`:git-<newsha>` tags from that branch's source** — but with
`PAPERCLIP_IMAGE` hardcoded as above, the deploy runs the specific **older**
tag you named, not the one it just built. A plain re-dispatch from an old
branch, without that edit, is not itself a rollback — it would build and deploy
a fresh image from old source under a new sha.

If the tag you name is *not* in the host's store, `pull_policy: never` makes
`compose up -d` fail with an image-not-found error rather than reaching for
GHCR. That is the intended behaviour: the alternative is silently starting
whatever the retired publish-then-pull scheme happened to leave under that tag.

## Required repository secrets (environment: `staging`)

The environment name is lowercase `staging` — GitHub Actions matches
environment names case-sensitively, so a `Staging` environment's secrets will
not resolve here.

| Secret | Notes |
|---|---|
| `SSH_PRIVATE_KEY_DEV` | Deploy user's private key |
| `HOST` | Staging host, used for `ssh-keyscan`, the paperclip-home guard's plain `ssh`, the disk-space and fingerprint reads, and the job-level `DOCKER_HOST: ssh://${USERNAME}@${HOST}:${SSH_PORT}` that every `docker` call — the build included — goes through |
| `USERNAME` | Deploy user on the host |
| `SSH_PORT` | Optional, defaults to 22. Threaded through every consumer that needs it: the `ssh-keyscan` that seeds `known_hosts`, the paperclip-home guard's `ssh` call, the job-level `DOCKER_HOST`, and the disk-space and fingerprint reads over `ssh` — a mismatch between any of these would scan or dial a different endpoint than the others |
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

**The generic charset guard is gone, and its removal is a consequence of the
no-env-file design rather than a relaxation.** The workflow used to reject any
secret containing `$`, a backtick or `#`, because Compose's `env_file` parser
interpolates the first two and treats `#` as a comment — so such a value
reached the container as a *different* string with nothing erroring. Nothing
dotenv-parses these values any more: the `Deploy` step binds them as its own
`env:` and Compose reads them from the process environment, where they are
taken literally. A `$` in a `BETTER_AUTH_SECRET` now survives verbatim.

**Two guards do remain**, and both are about what the value *means* rather than
how it is parsed. They run in `Verify the application secrets`, **before the
build**, since neither can produce a working deploy and finding that out after
a cold build wastes the whole run:

- `POSTGRES_PASSWORD` must be non-empty and must not contain `@ : / ? #`. The
  compose file interpolates it into
  `postgres://paperclip:${POSTGRES_PASSWORD}@postgres:5432/paperclip`, where
  those characters silently corrupt the connection string with no error from
  Compose. `openssl rand -hex 32` never produces any of them.
- `PAPERCLIP_PUBLIC_URL` must not be empty or contain `localhost`/`127.0.0.1`.
  It is baked into auth callbacks and shown to the first admin, so a localhost
  value produces an instance nobody outside the host can claim. It legitimately
  contains `:` and `/`, which is why the stricter `POSTGRES_PASSWORD` pattern
  must never be applied to it.

No secret is written to disk at any point — not on the runner, not on the
staging host — which is why there is no cleanup step to delete one.

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
`ssh`, right after the SSH key is loaded and *before* the build, `Verify host
preconditions` or anything else that touches the host, so a wrong path or owner
fails in seconds rather than after a full build on the staging host. They stay
on `ssh` rather than on the job-level `DOCKER_HOST` this step inherits, because
a `DOCKER_HOST` proxies the Docker API, not a shell, and cannot stat a path on
the host filesystem. The validator asserts both the `ssh` and the
before-the-build ordering:

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
to a service would quietly put it on the backend bridge. It also asserts
`pull_policy: never` on **paperclip** (its image is built straight into the
host's store and never pushed, so a fallback to GHCR could start a stale tag
from the retired publish-then-pull scheme) and the **absence** of any
`pull_policy` on **postgres** (`postgres:17-alpine` is an upstream image
nothing here builds, so `never` on it would break the first `up -d` on a host
that does not already have it).

Several workflow assertions below are **deliberate inversions** of the Phase 1
contract, when the image was built on the runner and pushed to GHCR. Back then
`DOCKER_HOST` had to be step-scoped, `--env-file` was mandatory and `compose
pull` had to precede `up -d`; each is now asserted the other way round, with
the reason recorded on the check itself. For the deploy workflow specifically:

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
- `DOCKER_HOST` is set at **job level** — the build runs on the deploy server,
  so every `docker` call in the job has to reach that daemon. It is **not** set
  at workflow level (that would apply to any job added later, including one
  with no business reaching the host), and **no step overrides it** — a
  step-level value wins over the job-level one and would silently point that
  single step at a different daemon than the build and the deploy;
- no step runs a `docker` command, or a `docker/*` action, **before**
  `Write SSH key and scan the host key`. Every `docker` call now goes over SSH
  via the job-level `DOCKER_HOST`, authenticated by the agent that step starts;
- the step running `docker/build-push-action` sets `push: false` (there is no
  registry in the loop) and pins `target: production` (the Dockerfile declares
  a later `cloud` stage, and omitting `target:` would silently deploy it);
- the step running `docker/setup-buildx-action` pins `driver: docker` — that
  binds the build to the remote daemon's own persistent layer store. The
  default `docker-container` driver creates a fresh builder with an **empty
  cache** on every dispatch, which is the cold 15–45 minute build this whole
  arrangement exists to avoid;
- no `docker login`, `docker logout`, or `docker/login-action` anywhere —
  nothing talks to a registry, and this runner shares `~/.docker/config.json`
  with sibling repos' concurrent deploys;
- the `Verify the paperclip-home guards` step reaches the host over plain
  `ssh`, reads ownership with `stat -Lc`, and runs **before** the build step —
  these checks stat a path on the host filesystem, which the job-level
  `DOCKER_HOST` (an API proxy, not a shell) cannot do, and a typo'd path must
  fail in seconds rather than after a 45-minute cold build;
- `bootstrap` is declared as a `workflow_dispatch` input, typed `boolean`,
  defaulting to `false`;
- no line writes raw `ssh-keyscan` output directly onto `known_hosts` outside
  `$RUNNER_TEMP`, whether via a bare `>` or piped through `tee`;
- the private key is never written to `~/.ssh/id_rsa` anywhere in the
  workflow, and no step names `id_rsa` **outside `$RUNNER_TEMP`** at all. This
  is what makes the removal of the `if: always()` cleanup step safe rather than
  an oversight: the key is the only secret still reaching disk, and the runner
  clears `$RUNNER_TEMP` at the start of each job. A key written anywhere else
  would survive the run and would need an explicit cleanup step back;
- `docker volume create` and any `mkdir` of the paperclip-home bind-mount
  source (by literal path, `$PAPERCLIP_HOME_DIR`, or `${PAPERCLIP_HOME_DIR}`)
  are each only reachable from inside a branch whose **enclosing** `if`/`elif`
  condition tests the `bootstrap` input — checked with an if/elif/else/fi-aware
  scan, not merely "does `bootstrap` appear earlier in the step", so an
  unconditional create placed *after* that branch's `fi` (i.e. no longer
  actually gated by anything) is still rejected;
- Compose is **never** invoked with `--env-file`, and no step writes an env
  file at all. Secrets reach Compose as the `Deploy` step's own `env:`, so
  nothing touches disk and nothing is dotenv-parsed;
- a step named `Deploy` exists and its `env:` declares **every** variable the
  compose file references. With no `--env-file` that step's own environment is
  the only source Compose can resolve them from: a `${VAR:?}` reference fails
  the deploy outright, and a plain `${VAR}` one renders as an empty string with
  nothing erroring;
- `compose pull` appears **nowhere**, and `compose up -d` does. The build puts
  the image directly into the host's image store, so there is nothing to pull —
  and GHCR still carries tags from the retired publish-then-pull scheme, so a
  pull would silently replace what this run built with a stale image.
  `pull_policy: never` in the compose file is the other half of that guard;
- every heredoc-fed `docker exec` passes `-i`, and no `docker exec` that is
  *not* heredoc-fed passes `-i` — a heredoc without `-i` gets no stdin, so the
  step's body never runs and it still exits 0. This repo's Postgres smoke
  check (`docker exec -e PGPASSWORD paperclip-postgres psql ...`) is the
  deliberate non-heredoc case proving the rule the other way.

It runs in the **TRC deploy checks** workflow. Read its docstring before
changing the compose file or the deploy workflow.
