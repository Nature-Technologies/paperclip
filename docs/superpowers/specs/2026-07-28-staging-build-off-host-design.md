# Move the staging build off the deploy host

**Date:** 2026-07-28
**Status:** Approved, not yet implemented
**Affects:** `.github/workflows/trc-staging-deploy.yml`, `deploy/trc/validate_compose.py`,
`deploy/trc/docker-compose-staging-trc.yml` (comment only), `docs/deploy/trc-staging.md`

## Problem

`trc-staging-deploy.yml` sets `DOCKER_HOST` at **job level**, so every `docker`
call in the job — the build included — runs against the staging host's daemon.
That was a deliberate choice: buildx's default container driver starts with an
empty cache every dispatch, and the host daemon's layer store was the only cache
that persisted between deploys.

It has three costs, all of them now being paid:

1. **Host disk.** BuildKit cache plus accumulating `:git-<sha>` images fill
   `/var/lib/docker` on the staging host. The 10 GB / 25 GB guards in `Verify
   host preconditions` trip, and clearing them by hand risks
   `docker image prune -a`, which destroys every rollback target.
2. **Contention.** The build's CPU and memory compete with the live `paperclip`
   and `paperclip-postgres` containers, so staging degrades while a deploy runs.
3. **Speed.** The host-side cache has not delivered the payoff it was adopted
   for; builds remain slow.

## Decision

Build on the self-hosted runner. Ship the finished image to the staging host as
a stream over the existing SSH connection. No registry.

`DOCKER_HOST` stops being job-level and becomes **step-scoped**: present on
every step that must reach the staging host, absent from the build.

### Why not a registry (GHCR)

The staging host has outbound access — it pulls `postgres:17-alpine` on every
deploy — so a build-push-pull scheme is technically available. It is rejected:

- It reverses a deliberate prior decision. `pull_policy: never` would have to
  go, reopening the exact failure the current comments describe: GHCR still
  carries tags from the retired publish-then-pull scheme, and a rollback
  dispatch could silently start one.
- The dedup payoff is largely illusory here. `COPY --from=build /app /app` in
  the Dockerfile collapses into a single enormous layer that changes on every
  build, so a registry pull would transfer nearly as much as a `docker save`.

Its one real advantage — rollback targets surviving a host rebuild — does not
outweigh reintroducing a registry's failure modes.

### Why not bound the host-side build in place

A BuildKit GC policy plus `--keep-storage` and a CPU cap is the smallest
possible diff, and addresses cost 1 and part of cost 2. It leaves the build on
the machine serving staging traffic and does nothing for cost 3. Rejected.

## Architecture

### Step sequence

Unchanged unless noted. Ordering constraints that already exist — SSH key step
first, both secret guards and the `paperclip-home` guards before the build —
all still hold.

| # | Step | Change |
|---|---|---|
| 1 | Checkout | — |
| 2 | Write SSH key and scan the host key | — |
| 3 | **Preflight: runner build capacity** | **new** |
| 4 | Verify the paperclip-home guards | — (plain SSH, as today) |
| 5 | Compute image tags | — |
| 6 | Verify the application secrets | — |
| 7 | Verify host preconditions | step-scoped `DOCKER_HOST`; thresholds reworded |
| 8 | Setup Node.js → Setup pnpm → Refresh lockfile | — |
| 9 | Set up Buildx | `driver: docker` now binds the **runner's** daemon |
| 10 | Build the image | no `DOCKER_HOST`; `push: false`, `target: production` |
| 11 | **Ship the image to the host** | **new** |
| 12 | **Confirm the image landed** | **new** |
| 13 | Deploy | step-scoped `DOCKER_HOST` |
| 14 | Smoke test | step-scoped `DOCKER_HOST` |
| 15 | **Host image retention** | **new**, non-fatal |
| 16 | **Runner cache cap** | **new**, non-fatal |

### Preflight: runner build capacity (step 3)

Runs before anything expensive, and answers the open question about what the
runner actually is:

- `docker version` with **no** `DOCKER_HOST` — proves a local daemon exists.
  Failure here is fatal and names the fallback (install `dockerd` on the runner,
  or introduce a dedicated build host).
- `nproc`, reported for information.
- Free disk on the runner's `/var/lib/docker`: **fail under 15 GB, warn under
  30 GB**. Cold-build headroom now lives here.

### Ship the image (step 11)

```sh
docker save "$SHA_TAG" "$STAGING_TAG" | DOCKER_HOST="ssh://…" docker load
```

Both tags reference the same image, so one stream carries both and layers are
shared. `docker load` with `DOCKER_HOST=ssh://…` streams the tar to the remote
daemon over the Docker CLI's own SSH transport, which means the staging host
needs **only `dockerd`** — no docker CLI, no `zstd`, no registry.

`DOCKER_HOST` is set **inline on the `load` side only**, never as this step's
`env:`. A step-level binding here would send `docker save` to the staging host
as well, and that failure is silent rather than loud: if a `:git-<sha>` image
already exists there, the pipeline would save and re-load *that* image — a
no-op that deploys whatever the host was already running instead of what this
run built. The validator asserts the inline form (see contract change 5).

Shipped uncompressed initially, deliberately: the transfer crosses a LAN link
whose speed is unmeasured, and compression may cost more CPU than it saves.
The documented fallback, if the first dispatch shows the transfer is slow, is:

```sh
docker save "$SHA_TAG" "$STAGING_TAG" | zstd -T0 \
  | ssh "$USERNAME@$HOST" 'zstd -d | docker load'
```

which requires `zstd` on both machines and a docker CLI on the host.

### Confirm the image landed (step 12)

`docker image inspect` over `DOCKER_HOST` against the `:git-<sha>` tag, before
compose is touched. A truncated or silently failed load must not reach
`compose up`.

### Host image retention (step 15)

Keep the newest **5** `:git-<sha>` images plus whatever the running `paperclip`
container resolves to; remove the rest. Explicitly **not** `docker image prune
-a`, which `docs/deploy/trc-staging.md` correctly identifies as the command
that destroys every rollback target. Dangling (`<none>`) images are safe to
remove and are included.

Warns but never fails: the deploy has already succeeded by this point, and a
cleanup problem must not mark it red.

### Runner cache cap (step 16)

`docker builder prune --keep-storage=30GB -f` on the runner's local daemon,
bounding the cache that now lives there so this change does not simply relocate
cost 1. Warns but never fails. 30 GB is a starting value, not a measured one —
retune it once a dispatch reports how large the cache actually gets.

### Threshold changes

Cold-build headroom moves from the host to the runner:

- **Runner** (new): fail under 15 GB, warn under 30 GB.
- **Host**: failure threshold stays at 10 GB — a `docker load` still needs room
  — but the message stops claiming a cold build needs the space. Warn drops
  from 25 GB to 20 GB.

All four are starting values, to be retuned once the first dispatch reports the
real image size and transfer time.

## Contract changes in `validate_compose.py`

The validator asserts the deploy workflow's structure, not just the compose
file, and runs on every PR via `trc-deploy-checks.yml`. It encodes the current
architecture and will reject this design until updated. **The validator and
workflow changes must land in the same commit** — either alone leaves the
branch red.

### Two checks invert

| Line | Today | Becomes |
|---|---|---|
| ~770 | `DOCKER_HOST` **must** be set at job level | **must not** be — job level is what drags the build onto the staging host |
| ~789 | step-level `DOCKER_HOST` is **banned** | **required**, on remote steps specifically (see classification below) |

This also resolves an existing contradiction: the `docker context create` ban
at ~654 already advises "use step-scoped `DOCKER_HOST` instead", which the
step-level ban forbids. That message becomes true.

### Every daemon-touching step is classified

With no job-level binding, each step that shells out to `docker` reaches one
daemon or the other, and which one must be asserted rather than inferred. The
validator carries an explicit list:

**Remote** — must set `DOCKER_HOST` in the step's own `env:`:
Verify host preconditions, Confirm the image landed, Deploy, Smoke test,
Host image retention.

**Local** — must **not** set `DOCKER_HOST` at all:
Preflight, Set up Buildx, Build, Runner cache cap.

**Split** — the transfer step, which is neither: it must not set `DOCKER_HOST`
in `env:`, and must carry it inline on the `docker load` side of the pipe.

A daemon-touching step matching none of the three is a failure. That way adding
a step later forces a decision about which daemon it talks to, instead of
silently inheriting one.

### Five checks are added

1. The `docker/build-push-action` step must have no `DOCKER_HOST` in scope.
   This is the assertion that encodes "the build does not run on the staging
   host".
2. A transfer step must exist between the build and the deploy: a `docker save`
   piped into a `docker load`.
3. The image-landed `docker image inspect` must precede `compose up`.
4. `docker image prune -a` must appear nowhere.
5. The transfer step must not set `DOCKER_HOST` in its `env:`, and the
   `docker load` in it must be prefixed by an inline `DOCKER_HOST=`. This
   catches the silent stale-deploy described under step 11.

### Unchanged, deliberately

No registry enters the loop, so none of the registry-related contract survives
by accident — it survives because it is still correct:

- `--env-file` ban, `compose pull` ban, `docker login` / `docker logout` /
  `docker/login-action` bans
- `docker/build-push-action` and `docker/setup-buildx-action` both required
- workflow-level `DOCKER_HOST` ban
- every `DOCKER_HOST` value must be built from `secrets.HOST` and
  `secrets.USERNAME` — `_docker_host_values()` already walks workflow, job and
  step scope, so this keeps working unmodified
- `docker context create` / `use` ban
- SSH-key-step-first ordering (its *rationale comment* changes: not every
  docker call goes over SSH any more, but every remote one and every plain
  `ssh` still does)
- external volume `docker volume inspect` guard
- `paperclip-home` guards and both secret guards before the build
- `pull_policy: never` on `paperclip`, absent on `postgres`

## Documentation

Four places describe the arrangement being retired:

- The header comment of `trc-staging-deploy.yml` — largely rewritten.
- `docs/deploy/trc-staging.md` — the "build runs ON the deploy host" section
  (~lines 13–100) and "Runner prerequisites" (~lines 239–257), which changes
  from *needs free disk on the staging host* to *needs a local daemon and free
  disk on itself*. The rollback section stays accurate — tags remain host-local
  — but should state that keep-N retention now bounds them.
- `validate_compose.py`'s module docstring — its "deliberate inversions"
  paragraph needs a third entry for this change.
- `docker-compose-staging-trc.yml` ~line 46 — "built straight into the deploy
  host's image store" becomes "built on the runner and loaded into the host's
  image store". `pull_policy: never` keeps its rationale unchanged.

### Migration note

After the switch, the staging host still holds the BuildKit cache the old
scheme accumulated. A one-time `docker builder prune -a` there reclaims it.
This is safe and is **not** `docker image prune -a`, which would destroy the
rollback targets.

## Failure modes

- **No local daemon on the runner** — preflight fails on the first dispatch,
  before the lockfile refresh or any build work. See Open risks.
- **Build fails** — nothing has touched the staging host; the running stack is
  untouched. Unchanged from today.
- **Transfer fails or the SSH connection drops mid-load** — the step fails
  before `compose up`. The previous image keeps running; re-dispatch. No
  retry logic: a partial load cannot be distinguished cheaply from a slow one,
  and step 12 catches whatever landed.
- **Load reports success but the image is absent** — step 12 catches it before
  compose is touched.
- **Retention or cache-cap problems** — warn only; the deploy has already
  succeeded.

The useful property preserved throughout: build and transfer both complete
before `compose up` runs, so a failure never leaves staging half-deployed.

## Verification

- `python deploy/trc/validate_compose.py` passes. This is the real test and it
  gates PRs through `trc-deploy-checks.yml`.
- The workflow parses as valid YAML.
- Nothing else can exercise a deploy workflow short of dispatching it. **The
  first dispatch is the acceptance test**, and it produces the numbers the
  thresholds and retention count should be retuned against: the runner's
  specs, the image size, and the transfer time.

## Open risks

**The runner's capabilities are unverified.** Whether it has a local Docker
daemon, and whether it has the disk to hold a build cache, is unknown at design
time. Everything here rests on that assumption. If it fails, the shape of the
design survives — build somewhere that is not the staging host, ship over SSH —
but the build moves to a dedicated host, and the preflight step becomes a
remote check rather than a local one. The preflight exists so this is answered
on the first dispatch, loudly and in seconds.

**Transfer time is unmeasured.** If it proves slow enough to matter, the zstd
variant above is the first remedy. A local registry on the runner, pulled by
the host, would restore layer dedup — but it depends on host→runner
reachability, which is likewise unverified, and it reintroduces a registry.
Neither is in scope until the first dispatch produces a number.

## Out of scope

- Reducing the image size, though the single-large-layer `COPY --from=build`
  is why registry dedup would not help.
- Any change to `pull_policy`, the compose topology, the secret-binding scheme,
  or the `hermes_gateway` fingerprint check.
- The sibling repos' deploys (`trc-hermes-agent`, `trc-open-webui`). They build
  on their own deploy hosts by the same mechanism, and the same reasoning would
  apply, but this spec changes only `trc-paperclip`.
