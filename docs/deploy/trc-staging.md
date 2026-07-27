# TRC staging deploy — paperclip

Deploys this fork's image plus its private Postgres to the TRC staging host as
one compose project, `trc-staging-paperclip`. `trc-hermes-agent` and
`trc-open-webui` deploy themselves the same way; all three attach to the shared
external `poc-net` bridge. Paperclip and Postgres deliberately stay **off**
`trc-shared` — only the chat services reach the trc-backend stack.

Bringing Paperclip up is deferrable: it is not required to unblock chat.

The deploy runs `docker compose` **from the GitHub Actions runner**, against
the host's Docker daemon over a remote Docker context
(`docker context create ... --docker "host=ssh://${USERNAME}@${HOST}:${SSH_PORT}"`).
It never opens an interactive SSH shell on the host to run compose commands.
Consequences of that:

- The rendered `.env.staging` file is read by the **local** Compose CLI via
  `--env-file` and is never copied to the server. Its values reach the
  containers as container environment, sent over the Docker API through the
  SSH tunnel — there is no plaintext secret file on the staging host.
  `.env.staging` is the only file this workflow ever writes to disk on the
  runner, and the final `Remove the deploy context` step deletes it (with
  `if: always()`, so a failed run still cleans up).
- This project has **no config bind mount** to copy — unlike `trc-hermes-agent`'s
  `config.yaml`, nothing needs to be `scp`'d onto the host for compose to
  start. But `/srv/trc/staging/paperclip-home` **is** a pre-existing bind mount
  this deploy never manages: a Docker context proxies the Docker API, not a
  shell, so it cannot stat a path on the host filesystem. The three guards on
  that directory (see below) therefore stay on plain SSH and run in the
  `Verify the paperclip-home guards` step, **before** the Docker context is
  even created.
- `docker compose pull` sends the **runner's** registry credentials to the
  remote daemon (`X-Registry-Auth`), not the host's — the staging host never
  **stores** a credential of its own. Its daemon does receive the
  `GITHUB_TOKEN` (on pull, and via `/auth` when `docker login` runs under the
  active remote context), but never persists it; the token dies with the run.
  The workflow logs in to `ghcr.io` on the runner with the built-in
  `GITHUB_TOKEN` before pulling; this is what replaces the old host-side
  `docker login` + `GHCR_READ_TOKEN` secret, and it works whether the package
  is public (as it is today) or private.

## Host prerequisites

The deploy creates **only** `poc-net`, idempotently. It creates neither
`trc-staging-paperclip-db` nor `/srv/trc/staging/paperclip-home`, and it fails
if either is missing or wrong:

- **`trc-staging-paperclip-db` must already exist.** The deploy deliberately
  does not run `docker volume create`: the volume is declared `external: true`
  precisely so Compose refuses to start without it. Pre-creating it on every
  dispatch would let Postgres initialise a brand-new database against a
  silently empty volume, with Paperclip coming up with no data and every smoke
  test still passing. Create it once, by hand, during Phase 2.
- **`/srv/trc/staging/paperclip-home` must already exist and be owned by UID
  1000.** See the section on it below. No `trc-shared` check exists here at
  all — paperclip and postgres are `poc-net`-only by design.

The deploy user (`USERNAME`) needs:

- **read access to `/srv/trc/staging/paperclip-home`** (for the guard checks)
  and to `/srv/trc/staging/fingerprints` (to read hermes-agent's published
  fingerprint during the smoke test);
- **membership of the `docker` group** (or root) on the host, so the SSH
  session backing the Docker context can reach the daemon socket without
  `sudo`.

### Phase 2 preconditions

**Paperclip starts fresh.** Unlike hermes-agent and open-webui, it needs no data
migrated, so Phase 2 for this repo is only:

1. `docker volume create trc-staging-paperclip-db` — empty is correct here.
2. `mkdir -p /srv/trc/staging/paperclip-home && chown 1000:1000
   /srv/trc/staging/paperclip-home` — empty is correct here too.
3. Dispatch the deploy **once** with `allow_bootstrap=true`.

## Running a deploy

The workflow tracks a **moving tag**, not a digest. `trc-publish.yml` builds
and publishes `ghcr.io/nature-technologies/trc-paperclip:dev` (plus an
immutable `sha-<short-sha>` tag) on every push to `dev` (and to
`chore/new-ci`, while that branch carries CI on its own). It builds the
Dockerfile's `production` target explicitly — the Dockerfile also declares a
later `cloud` stage, and omitting `target:` would silently publish that
variant instead. The deploy workflow's `env:` block pins which tag it rolls
out:

```yaml
env:
  IMAGE_TAG: dev
```

Change that one line to follow a different branch tag; nothing else in the
workflow needs to change.

1. Actions → **TRC staging deploy (paperclip)** → Run workflow. Leave
   `allow_bootstrap` as `false` unless this is a deliberate first deploy of a
   fresh instance (see below).
2. The workflow pulls `${IMAGE_REPO}:${IMAGE_TAG}` and deploys it.

### Rolling back

**There is no digest input any more.** Rolling back means pointing `IMAGE_TAG`
at the `sha-<short-sha>` tag of the build you want (visible in the
`trc-publish.yml` run history or in the GHCR package's tag list). Those
immutable `sha-` tags exist precisely to make this possible — `:dev` moves, so
it cannot name a previous build.

`IMAGE_TAG` is workflow-level `env`, not a dispatch input, so changing it
requires a **commit**. Do **not** make that commit on `dev`:

> Pushing an `IMAGE_TAG` change to `dev` re-triggers `trc-publish.yml`, which
> takes 45–60 minutes and **republishes `:dev` from the same source** — you
> would rebuild the very image you are trying to roll away from, and overwrite
> the tag in the process.

Instead, dispatch the deploy from a throwaway branch:

1. Branch off the current `dev` (name it anything that is not `dev`, e.g.
   `rollback/2026-07-27`).
2. Edit the one line — `IMAGE_TAG: sha-<short-sha>` — commit, and push the
   branch. No publish is triggered, because `trc-publish.yml` only fires on
   `dev` and `chore/new-ci`.
3. Actions → **TRC staging deploy (paperclip)** → Run workflow, and select
   **that branch** as the workflow ref (the "Use workflow from" selector). The
   deploy is `workflow_dispatch`-only, so it runs the workflow definition from
   whichever ref you pick, including its `IMAGE_TAG`. Leave `allow_bootstrap`
   as `false`.
4. Delete the branch once you are done. To roll forward again, dispatch the
   deploy from `dev` as normal.

The `Pull and deploy` step still prints the digest that actually landed, so
every run log records what is now running.

## Required repository secrets (environment: `Staging`)

| Secret | Notes |
|---|---|
| `SSH_PRIVATE_KEY_DEV` | Deploy user's private key |
| `HOST` | Staging host, used both for `ssh-keyscan`, the paperclip-home guard's plain `ssh`/the fingerprint read, and the Docker context target (`ssh://${USERNAME}@${HOST}:${SSH_PORT}`) |
| `USERNAME` | Deploy user on the host |
| `TRC_SSH_PORT` | Optional, defaults to 22. Threaded through every consumer that needs it: the `ssh-keyscan` that seeds `known_hosts`, the paperclip-home guard's `ssh` call, the Docker context's `ssh://${USERNAME}@${HOST}:${SSH_PORT}` target, and the smoke test's fingerprint read over `ssh` — a mismatch between any of these would scan or dial a different endpoint than the others |
| `POSTGRES_PASSWORD` | Also interpolated into `DATABASE_URL`. The workflow rejects an empty value or one containing `@ : / ? #`, since those characters silently corrupt the connection string with no error from compose |
| `BETTER_AUTH_SECRET`, `PAPERCLIP_TOOL_ACTION_SIGNING_SECRET` | `openssl rand -hex 32` |
| `OPENROUTER_API_KEY` | |
| `PAPERCLIP_PUBLIC_URL` | The external URL a browser can reach. The workflow rejects `localhost`/`127.0.0.1` — this value is baked into auth callbacks and shown to the first admin |

`TRC_SSH_HOST`/`TRC_SSH_USER`/`TRC_SSH_KEY`/`TRC_SSH_KNOWN_HOSTS`/`GHCR_READ_TOKEN`
no longer exist as secrets, from the retired scp-based deploy. Host keys are
**scanned at deploy time** (`ssh-keyscan -p "$SSH_PORT" -H "$HOST" >
~/.ssh/known_hosts`) rather than pinned in advance:

- This still protects against a passive attacker who cannot intercept the very
  first connection of a run — `StrictHostKeyChecking` is never disabled, so if
  the host key changes *after* the scan (e.g. mid-run, or on a subsequent run
  against a key that was swapped since the last scan and cached nowhere) the
  connection still aborts rather than silently trusting a new key.
- It does **not** protect against an active machine-in-the-middle present at
  the moment `ssh-keyscan` runs, since there is no prior pinned key to compare
  against — trust-on-first-use accepts whatever key answers on that first
  connection. This is a deliberate trade against the operational cost of
  maintaining a `TRC_SSH_KNOWN_HOSTS` secret in step with any host-key
  rotation; the previous pinned-key model traded the other way.

Every secret rendered into `.env.staging` is charset-guarded: the workflow
rejects any value containing `$`, a backtick or `#`. Compose's `env_file` parser
interpolates the first two and treats `#` as a comment, so such a value would
reach the container as a *different* string with nothing erroring — a mangled
`BETTER_AUTH_SECRET` looks like auth cookies that never validate, a mangled
`PAPERCLIP_TOOL_ACTION_SIGNING_SECRET` looks like tool actions rejected as
unsigned. `openssl rand -hex 32` never produces any of them. This guard applies
to `POSTGRES_PASSWORD` too — Compose interpolates a `$` in that value exactly
the same as any other, and it is additionally interpolated into `DATABASE_URL`,
where `@ : / ?` also corrupt the connection string, so `POSTGRES_PASSWORD`
carries **both** guards. `PAPERCLIP_PUBLIC_URL` is checked for `$`/backtick/`#`
only — it legitimately contains `:` and `/`, so the stricter
`POSTGRES_PASSWORD` pattern must not be applied to it. `.env.staging` itself is
written on the runner and passed to Compose with `--env-file`; it is never
copied to the host.

The Postgres smoke check runs a real authenticated `select 1` rather than
`pg_isready`, which never authenticates and would return success against a
password it had never seen. It connects with `-h postgres` (the compose
service alias over `poc-net`), never the unix socket or `127.0.0.1`: this
image's generated `pg_hba.conf` grants `trust` on both of those, so a wrong
password would return exit 0 and the check would prove nothing.

## `/srv/trc/staging/paperclip-home` — read this before deploying

The bind mount at `/paperclip` holds `instances/default/`: `workspaces/`,
`data/run-logs/`, `data/backups/`, `logs/`, `config.json`, and
`secrets/master.key`.

**Nothing is migrated into it. Paperclip starts fully fresh in Phase 2** — do
**not** copy the old Postgres volume, and do **not** copy the old
`./paperclip-data` directory from the retired stack. Postgres ignores
`POSTGRES_PASSWORD` on an already-initialised data directory, so a copied volume
would leave the freshly generated secret unable to authenticate against it; a
fresh cluster initialises with the new secret instead, which is why
`POSTGRES_PASSWORD` stays a rotatable repository secret and no `ALTER USER` step
is needed.

Starting fresh means the first admin opens `PAPERCLIP_PUBLIC_URL`, creates an
account, chooses **Claim this instance**, and then **re-enters the Hermes gateway
wiring by hand** — the old `instances/default/config.json` is not being carried
over, so its `hermes_gateway` URL and API key have to be set again. Use
`http://hermes-agent:8642` with the same `HERMES_API_KEY` value the other two
repos deploy; the smoke test fingerprint-compares it against
`trc-hermes-agent`'s copy and warns on a mismatch.

Three guards still apply, and they run in the `Verify the paperclip-home
guards` step — over plain SSH, and *before* the Docker context is created or
any secret is rendered, so a wrong path or owner cannot leave a rendered
`.env.staging` half-applied:

- **The deploy will never create the directory**, in any mode, not even with
  `allow_bootstrap=true`. It holds `secrets/master.key` and the only copy of the
  `hermes_gateway` wiring, so a typo'd path must fail loudly rather than be
  silently accepted as a fresh install. The workflow fails if it is absent, and
  the validator fails if anyone adds an `mkdir` for it — by literal path or via
  `$PAPERCLIP_HOME_DIR`/`${PAPERCLIP_HOME_DIR}`. Never substitute a
  bind-mounted probe container for this check either: Docker creates a missing
  bind source as root, which is exactly the failure the guard exists to
  prevent.
- **It must be owned by UID 1000.** Bind mounts carry host ownership straight
  through and the container runs as `node`, so without `chown -R 1000:1000` the
  first write to `/paperclip` fails with `EACCES` and the container crash-loops.
  The workflow checks the owner with `stat -Lc '%u'` (the `-L` dereferences
  symlinks, since both `[ -d ]` and Docker's bind mount follow them) and
  refuses to deploy otherwise. This check is unconditional — `allow_bootstrap`
  never relaxes it.
- **It must hold instance state — unless `allow_bootstrap=true`.** Normally a
  present-but-empty directory would defeat the first guard just as effectively
  as a missing one, so the workflow requires at least one of
  `instances/default/config.json` or `secrets/master.key`. On a deliberate fresh
  start the directory *is* legitimately empty, so this one check — and only this
  one — is relaxed by the `allow_bootstrap` input. **Needed for the first deploy
  only**; leave it `false` afterwards, or an accidentally emptied
  `paperclip-home` stops being caught.

`config.json` carries the `hermes_gateway` adapter's URL and API key — there is
no `HERMES_*` environment variable in the compose file, and that is not an
omission. It is read-only after the instance is claimed, so the deploy can only
fingerprint-check that key against `trc-hermes-agent`'s copy, not set it.

## First-run bootstrap

There is no CLI invite flow. For `authenticated`/`private` mode the first admin
opens `PAPERCLIP_PUBLIC_URL` in a browser, signs in or creates an account, and
chooses **Claim this instance**. Dispatch that first deploy with
`allow_bootstrap=true`.

## Checks

`deploy/trc/validate_compose.py` asserts the invariants that make three
independent compose projects add up to one stack — `external: true` on every
network and volume, `trc-shared` staying absent, Postgres publishing no ports,
and the `depends_on: postgres` ordering being kept. Each **service's** own
`networks:` membership is asserted too, not just the top-level keys, since a
top-level network no service joins is silently ignored and an extra one added
to a service would quietly put it on the backend bridge. For the deploy
workflow specifically it also asserts:

- the deploy runs against a remote Docker context (`docker context create`),
  never a raw SSH shell — that is what keeps the rendered `.env.staging` off
  the server;
- the deploy verifies `trc-staging-paperclip-db` with `docker volume inspect`
  and refuses to run if it is absent, rather than ever running
  `docker volume create` (see Host prerequisites);
- specifically the step that runs `docker context create` sources its `HOST`
  and `USERNAME` from those secrets and actually references them in its
  script — scoped to that one step, so hardcoding the host there while
  `secrets.HOST` is merely referenced somewhere else in the file (e.g. the
  host-key-scan step) does not pass;
- Compose is invoked with `--env-file`, so secret values are never
  interpolated into a shell command string;
- `compose pull` precedes `compose up -d` (same-line `pull && up -d` counts,
  compared by position within the line), so a deploy can never silently
  redeploy an image already on the host;
- `workflow_dispatch` still takes the `allow_bootstrap` boolean input,
  defaulting to `false` — the one input that survives the move away from
  digest pinning, gating only the instance-state check above;
- no `docker volume create` anywhere, and nothing in the workflow creates the
  `/srv/trc/staging/paperclip-home` bind mount — by literal path or via
  `$PAPERCLIP_HOME_DIR`/`${PAPERCLIP_HOME_DIR}`.

It runs in the **TRC deploy checks** workflow. Read its docstring before
changing the compose file or the deploy workflow.
