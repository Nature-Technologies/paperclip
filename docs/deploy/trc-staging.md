# TRC staging deploy — paperclip

Deploys this fork's image plus its private Postgres to the TRC staging host as
one compose project, `trc-staging-paperclip`. `trc-hermes-agent` and
`trc-open-webui` deploy themselves the same way; all three attach to the shared
external `poc-net` bridge. Paperclip and Postgres deliberately stay **off**
`trc-shared` — only the chat services reach the trc-backend stack.

Bringing Paperclip up is deferrable: it is not required to unblock chat.

## Host prerequisites

The deploy creates **only** `poc-net`, idempotently. It creates neither
`trc-staging-paperclip-db` nor `/srv/trc/staging/paperclip-home`, and it fails
if either is missing:

- **`trc-staging-paperclip-db` must already exist.** The deploy deliberately
  does not run `docker volume create`: the volume is declared `external: true`
  precisely so Compose refuses to start without it. Pre-creating it on every
  dispatch would let Postgres initialise a brand-new database against a silently
  empty volume, with Paperclip coming up with no data and every smoke test still
  passing. Create it once, by hand, during Phase 2.
- **`/srv/trc/staging/paperclip-home` must already exist and be owned by UID
  1000.** See the section on it below.

The deploy user also needs all of:

- **write access to `/srv/trc`** (the deploy `mkdir -p`s
  `/srv/trc/staging/paperclip` and `/srv/trc/staging/fingerprints`);
- **membership of the `docker` group**, so `docker` works without `sudo`;
- **permission to create `/var/lock/trc-deploy.lock`.** `/var/lock` is
  root-owned `0755` on some images, in which case `flock` fails *after* the
  `.env` has already been copied to the host. Either grant write access to
  `/var/lock` or pre-create the lock file owned by the deploy user.

### Phase 2 preconditions

**Paperclip starts fresh.** Unlike hermes-agent and open-webui, it needs no data
migrated, so Phase 2 for this repo is only:

1. `docker volume create trc-staging-paperclip-db` — empty is correct here.
2. `mkdir -p /srv/trc/staging/paperclip-home && chown 1000:1000
   /srv/trc/staging/paperclip-home` — empty is correct here too.
3. Dispatch the deploy **once** with `allow_bootstrap=true`.

## Running a deploy

1. Find the digest:
   `docker buildx imagetools inspect ghcr.io/nature-technologies/trc-paperclip:<tag> --format '{{.Manifest.Digest}}'`
   (the repository was renamed from `paperclip` to `trc-paperclip`; its
   publish workflow derives the GHCR package from `github.repository`, so
   current builds land under `trc-paperclip` — the old `paperclip` package is
   frozen history)
2. Actions → **TRC staging deploy (paperclip)** → Run workflow → paste the
   `sha256:...` digest.

Deploys are digest-pinned. **To roll back, re-dispatch with the previous
digest** — the deploy step prints the currently-running digest before replacing
it.

## Required repository secrets (environment: `staging`)

| Secret | Notes |
|---|---|
| `TRC_SSH_HOST`, `TRC_SSH_USER`, `TRC_SSH_KEY` | Deploy user and its private key |
| `TRC_SSH_KNOWN_HOSTS` | Pinned host key. The workflow fails if empty |
| `TRC_SSH_PORT` | Optional, defaults to 22 |
| `POSTGRES_PASSWORD` | Also interpolated into `DATABASE_URL`. The workflow rejects an empty value or one containing `@ : / ? #`, since those characters silently corrupt the connection string with no error from compose |
| `BETTER_AUTH_SECRET`, `PAPERCLIP_TOOL_ACTION_SIGNING_SECRET` | `openssl rand -hex 32` |
| `OPENROUTER_API_KEY` | |
| `PAPERCLIP_PUBLIC_URL` | The external URL a browser can reach. The workflow rejects `localhost`/`127.0.0.1` — this value is baked into auth callbacks and shown to the first admin |
| `GHCR_READ_TOKEN` | Optional. Only if the GHCR package is private |

Every secret rendered into the host `.env` is charset-guarded: the workflow
rejects any value containing `$`, a backtick or `#`. Compose's `env_file` parser
interpolates the first two and treats `#` as a comment, so such a value would
reach the container as a *different* string with nothing erroring. This applies
to `PAPERCLIP_PUBLIC_URL` too, which is checked for those three characters
**only** — it legitimately contains `:` and `/`, so the stricter
`POSTGRES_PASSWORD` pattern must not be applied to it. `POSTGRES_PASSWORD` keeps
both guards, because it is additionally interpolated into `DATABASE_URL`.

The Postgres smoke check runs a real authenticated `select 1` rather than
`pg_isready`, which never authenticates and so returned success against a
password it had never seen.

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

Three guards still apply, and they run in `Copy deploy artifacts` — *before* the
`.env` is copied to the host, so a wrong path or owner cannot leave secrets
behind:

- **The deploy will never create the directory**, in any mode, not even with
  `allow_bootstrap=true`. It holds `secrets/master.key` and the only copy of the
  `hermes_gateway` wiring, so a typo'd path must fail loudly rather than be
  silently accepted as a fresh install. The workflow fails if it is absent, and
  the validator fails if anyone adds an `mkdir` for it — by literal path or via
  `$PAPERCLIP_HOME_DIR`.
- **It must be owned by UID 1000.** Bind mounts carry host ownership straight
  through and the container runs as `node`, so without `chown -R 1000:1000` the
  first write to `/paperclip` fails with `EACCES` and the container crash-loops.
  The workflow checks the owner and refuses to deploy otherwise. This check is
  also unconditional.
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
and the `depends_on: postgres` ordering being kept. It also asserts the deploy
workflow's own invariants: `workflow_dispatch`-only with a required
`image_digest`, strict mode without tracing, a host-side `flock` whose `-c`
string begins with `set -e` (it is a separate shell, so the outer `set -eu` does
not reach into it), pinned SSH host keys, no `docker volume create` anywhere, and
that nothing in the workflow creates the `/srv/trc/staging/paperclip-home` bind
mount — by literal path or via `$PAPERCLIP_HOME_DIR`. Each **service's** own
`networks:` membership is asserted too, not just the top-level keys, since a
top-level network no service joins is silently ignored and an extra one added to
a service would quietly put it on the backend bridge. It runs in the **TRC
deploy checks** workflow. Read its docstring before changing the compose file or
the deploy workflow.
