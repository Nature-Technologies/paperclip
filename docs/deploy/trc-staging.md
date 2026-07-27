# TRC staging deploy — paperclip

Deploys this fork's image plus its private Postgres to the TRC staging host as
one compose project, `trc-staging-paperclip`. `trc-hermes-agent` and
`trc-open-webui` deploy themselves the same way; all three attach to the shared
external `poc-net` bridge. Paperclip and Postgres deliberately stay **off**
`trc-shared` — only the chat services reach the trc-backend stack.

Bringing Paperclip up is deferrable: it is not required to unblock chat.

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

## `/srv/trc/staging/paperclip-home` — read this before deploying

The bind mount at `/paperclip` holds `instances/default/`: `workspaces/`,
`data/run-logs/`, `data/backups/`, `logs/`, `config.json`, and
`secrets/master.key`. Two things follow:

- **The deploy will not create it.** An empty directory lets Paperclip
  bootstrap a fresh *unclaimed* instance over data that should have been
  migrated. The workflow fails if the directory is absent, and the validator
  fails if anyone adds an `mkdir` for it.
- **It must be owned by UID 1000.** Bind mounts carry host ownership straight
  through and the container runs as `node`, so without `chown -R 1000:1000` the
  first write to `/paperclip` fails with `EACCES` and the container crash-loops.
  The workflow checks the owner and refuses to deploy otherwise.
- **It must not be merely present and empty.** A directory that exists and is
  correctly owned but holds no instance state would defeat the point of the
  first guard just as effectively as a missing directory: Paperclip would
  still bootstrap a fresh *unclaimed* instance. The workflow also refuses to
  deploy unless at least one of `instances/default/config.json` or
  `secrets/master.key` is present.

`config.json` also carries the `hermes_gateway` adapter's URL and API key —
there is no `HERMES_*` environment variable in the compose file, and that is
not an omission. It is read-only after the instance is claimed, so the deploy
can only fingerprint-check that key against `trc-hermes-agent`'s copy, not set
it.

## First-run bootstrap

There is no CLI invite flow. For `authenticated`/`private` mode the first admin
opens `PAPERCLIP_PUBLIC_URL` in a browser, signs in or creates an account, and
chooses **Claim this instance**.

## Checks

`deploy/trc/validate_compose.py` asserts the invariants that make three
independent compose projects add up to one stack — `external: true` on every
network and volume, `trc-shared` staying absent, Postgres publishing no ports,
and the `depends_on: postgres` ordering being kept. It also asserts the deploy
workflow's own invariants: `workflow_dispatch`-only with a required
`image_digest`, strict mode without tracing, a host-side `flock`, pinned SSH
host keys, and that nothing in the workflow creates the
`/srv/trc/staging/paperclip-home` bind mount. It runs in the **TRC deploy
checks** workflow. Read its docstring before changing the compose file or the
deploy workflow.
