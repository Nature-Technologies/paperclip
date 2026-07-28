# Staging Build Off Host — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the trc-paperclip staging build off the deploy host onto the self-hosted runner, and ship the finished image to the host with `docker save | docker load` over the existing SSH transport.

**Architecture:** `DOCKER_HOST` stops being job-level and becomes step-scoped, which is what forces the build local while keeping every remote operation remote. A new transfer step streams the image to the staging host's daemon. `deploy/trc/validate_compose.py` asserts the workflow's structure and currently encodes the arrangement being retired, so it is both the thing to change and the test that proves each change.

**Tech Stack:** GitHub Actions (self-hosted runner), Docker + Buildx, Docker Compose, Python 3 + PyYAML (the validator), pnpm.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-07-28-staging-build-off-host-design.md`. Read it before starting.
- **The test command is `python deploy/trc/validate_compose.py`.** It must exit 0 at the end of every task. On Windows use `py` if `python` is not on PATH.
- The validator shells out to `docker compose config`. If Docker is unavailable locally, `check_compose_renders` fails for environmental reasons — that failure is *not* yours; the PR check in `.github/workflows/trc-deploy-checks.yml` is authoritative. Every other check runs without Docker.
- **Never `set -x`** in any step of the deploy workflow — it prints secrets into the log.
- **Never write a secret to disk** on the runner or the host. Secrets are bound as step `env:` and read from the process environment.
- **No registry.** Nothing may add `docker login`, `docker/login-action`, `docker compose pull`, or `--env-file`. The validator already bans all four; keep it that way.
- **Never `docker image prune -a`** in a run block. `:git-<sha>` tags are host-local and are the only rollback targets that exist.
- Every `DOCKER_HOST` / `REMOTE_DOCKER_HOST` value must be built from `secrets.HOST` and `secrets.USERNAME`, never a literal.
- Step ordering that must not change: `Write SSH key and scan the host key` runs before any step that shells out to `docker` or `ssh`; `Verify the paperclip-home guards` and `Verify the application secrets` both run before the build.
- Commit messages end with:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `.github/workflows/trc-staging-deploy.yml` | The build + deploy pipeline | Restructured: build goes local, transfer + verify + cleanup steps added |
| `deploy/trc/validate_compose.py` | Asserts the compose file **and the workflow's structure**; runs on every PR | Two checks invert, five added, helpers extended |
| `deploy/trc/docker-compose-staging-trc.yml` | The staging stack | Comment only — one stale sentence |
| `docs/deploy/trc-staging.md` | Operator documentation | Sections describing the host-side build rewritten |

Branch: `ci/build-off-staging-host` (already created, spec already committed on it).

---

### Task 1: Move the build to the runner and ship the image over SSH

This is the atomic core: the build going local and the image shipping must land together, because a local build with no transfer would deploy an image the host does not have.

**Files:**
- Modify: `deploy/trc/validate_compose.py` (helpers ~353-375, checks ~760-795)
- Modify: `.github/workflows/trc-staging-deploy.yml` (job `env:` ~122-127, steps from ~295 on)
- Test: `python deploy/trc/validate_compose.py`

**Interfaces:**
- Consumes: `steps.tags.outputs.sha` and `steps.tags.outputs.staging` from the existing `Compute image tags` step; workflow-level `IMAGE` and `COMPOSE_FILE_PATH` env.
- Produces: step names later tasks key on — `Build the image on the runner`, `Ship the image to the host`, `Confirm the image landed on the host`. Validator constants `LOCAL_DAEMON_STEPS`, `REMOTE_DAEMON_STEPS`, `TRANSFER_STEP`, `REMOTE_HOST_ENV_KEYS`, which Tasks 2-4 add names to.

- [ ] **Step 1: Widen the endpoint-key helper**

The transfer step cannot put its endpoint in an env key named `DOCKER_HOST` — that would send `docker save` to the staging host too. It uses `REMOTE_DOCKER_HOST`, which must still be covered by the "built from both secrets" check.

In `deploy/trc/validate_compose.py`, add the constant next to the other module constants (near `EXPECTED_SERVICE_NETWORKS`, ~line 64):

```python
# Env keys that carry a remote daemon endpoint. `DOCKER_HOST` binds a whole
# step to the staging host; `REMOTE_DOCKER_HOST` is the transfer step's
# endpoint, bound INLINE on the `docker load` side only (see TRANSFER_STEP).
# Both must be built from secrets rather than hardcoded, so both are collected
# here -- but only the exact name `DOCKER_HOST` classifies a step as remote.
REMOTE_HOST_ENV_KEYS = ("DOCKER_HOST", "REMOTE_DOCKER_HOST")
```

Then change `_docker_host_values()` (~line 353) to key on that tuple. Replace its three `if key == "DOCKER_HOST":` conditions with `if key in REMOTE_HOST_ENV_KEYS:` and replace its docstring's second paragraph with:

```python
    """Every remote-endpoint value in the workflow, with where it was found.

    Keyed on REMOTE_HOST_ENV_KEYS, so a sibling variable that merely contains
    the string "DOCKER_HOST" does not dilute these assertions, while the
    transfer step's REMOTE_DOCKER_HOST is still held to the same
    built-from-secrets rule.
    """
```

- [ ] **Step 2: Add the step classification constants**

Immediately after `REMOTE_HOST_ENV_KEYS`, add:

```python
# With no job-level DOCKER_HOST, every step that shells out to `docker`
# reaches one daemon or the other, and which one has to be asserted rather
# than inferred. A daemon-touching step in none of these three groups fails
# the validator, so adding a step later forces the decision.
#
# LOCAL: the runner's own daemon. Must NOT set DOCKER_HOST -- the build
# staying here is the entire point of this arrangement.
LOCAL_DAEMON_STEPS = {
    "Build the image on the runner",
    "Set up Buildx",
}
# REMOTE: the staging host's daemon. Each must set its own DOCKER_HOST.
REMOTE_DAEMON_STEPS = {
    "Confirm the image landed on the host",
    "Deploy",
    "Smoke test",
    "Verify host preconditions",
}
# Neither: `docker save` runs locally and pipes into a `docker load` that is
# bound to the host INLINE. A step-level DOCKER_HOST here would send the save
# to the host as well -- and that fails SILENTLY, because a `:git-<sha>` image
# already present there would be saved and re-loaded, deploying whatever the
# host was already running instead of what this run built.
TRANSFER_STEP = "Ship the image to the host"
```

- [ ] **Step 3: Invert the two DOCKER_HOST checks and add the classification check**

In `check_deploy_workflow()`, replace the `job_level_docker_host` and `step_level_docker_host` blocks (~lines 765-795, from the `job_level_docker_host = [` assignment through the end of the `check(not step_level_docker_host, ...)` call) with:

```python
    # Inverted: DOCKER_HOST was REQUIRED here while the build ran on the deploy
    # host. It is banned now for exactly the same reason it was required then
    # -- a job-level binding puts every docker call in the job, the build
    # included, on the staging host, and moving the build off that machine is
    # the entire point. Workflow level stays banned as it always was.
    job_level_docker_host = [
        name for name, job in (doc.get("jobs") or {}).items()
        if "DOCKER_HOST" in set(((job or {}).get("env")) or {})
    ]
    check(
        not job_level_docker_host,
        f"jobs {job_level_docker_host} set DOCKER_HOST at JOB level -- that "
        "puts every docker call in the job on the staging host, the build "
        "included, which is the arrangement this replaced. The build has to "
        "run on the runner, so bind DOCKER_HOST per step instead",
    )
    check(
        "DOCKER_HOST" not in set(doc.get("env") or {}),
        "DOCKER_HOST must not be set at WORKFLOW level -- that would apply to "
        "every job added later too, including ones with no business reaching "
        "the deploy host. Scope it to the individual steps",
    )

    # Inverted with it: a step-level DOCKER_HOST was banned as an override of
    # the job-level binding. With no job-level binding there is nothing to
    # override, and it is now the ONLY thing that sends a step to the host.
    for _, step in _steps_with_index(doc):
        if not _step_touches_daemon(step):
            continue
        step_name = (step or {}).get("name")
        binds_host = "DOCKER_HOST" in _step_env_keys(step)
        if step_name == TRANSFER_STEP:
            check(
                not binds_host,
                f"the {step_name!r} step sets DOCKER_HOST in its `env:` -- that "
                "would send `docker save` to the staging host too, and it fails "
                "SILENTLY: an image already there would be saved and re-loaded, "
                "deploying what the host was already running instead of what "
                "this run built. Bind it inline on the `docker load` side only",
            )
        elif step_name in LOCAL_DAEMON_STEPS:
            check(
                not binds_host,
                f"the {step_name!r} step sets DOCKER_HOST -- it must run "
                "against the RUNNER's own daemon. Moving the build off the "
                "staging host is what this workflow exists to do",
            )
        elif step_name in REMOTE_DAEMON_STEPS:
            check(
                binds_host,
                f"the {step_name!r} step drives the staging host's daemon but "
                "sets no DOCKER_HOST in its `env:` -- there is no job-level "
                "binding to inherit any more, so it would silently run against "
                "the runner's own daemon",
            )
        else:
            check(
                False,
                f"the {step_name!r} step shells out to docker but is in none of "
                "LOCAL_DAEMON_STEPS, REMOTE_DAEMON_STEPS or TRANSFER_STEP. "
                "Every daemon-touching step must declare which daemon it talks "
                "to -- there is no job-level DOCKER_HOST to fall back on",
            )
```

- [ ] **Step 4: Add the build-is-local, transfer-exists and ordering checks**

Append to `check_deploy_workflow()`, after the block from Step 3:

```python
    # The single assertion that encodes "the build does not run on the staging
    # host". The job-level ban above is necessary but not sufficient: a step
    # env: on the build step alone would put it back.
    build_step = next(
        (
            step for _, step in _steps_with_index(doc)
            if "docker/build-push-action" in str((step or {}).get("uses") or "")
        ),
        None,
    )
    check(
        build_step is not None and "DOCKER_HOST" not in _step_env_keys(build_step),
        "the docker/build-push-action step must not set DOCKER_HOST -- the "
        "build runs on the runner now, against its own persistent layer store",
    )

    transfer_step = _step_by_name(doc, TRANSFER_STEP)
    check(
        transfer_step is not None,
        f"no step named {TRANSFER_STEP!r} -- the build no longer runs on the "
        "deploy host, so the image has to be streamed there before compose can "
        "start it. Without this step `up -d` finds no image and pull_policy: "
        "never fails it closed",
    )
    if transfer_step is not None:
        transfer_lines = _script_lines(transfer_step.get("run") or "")
        piped = [
            ln for ln in transfer_lines
            if "docker save" in ln and "docker load" in ln
        ]
        check(
            bool(piped),
            f"the {TRANSFER_STEP!r} step must pipe `docker save` straight into "
            "`docker load` -- streaming to the remote daemon means the image "
            "never lands on the runner's disk as a tarball",
        )
        check(
            all(
                re.search(r"\|\s*DOCKER_HOST=\S+\s+docker\s+load", ln)
                for ln in piped
            ),
            f"the `docker load` in {TRANSFER_STEP!r} must be prefixed by an "
            "INLINE DOCKER_HOST= binding. Inline is what keeps `docker save` "
            "on the runner while the load goes to the host",
        )

    build_index = _build_step_index(doc)
    transfer_index = _step_index(doc, TRANSFER_STEP)
    landed_index = _step_index(doc, "Confirm the image landed on the host")
    deploy_index = _step_index(doc, "Deploy")
    check(
        None not in (build_index, transfer_index, landed_index, deploy_index)
        and build_index < transfer_index < landed_index < deploy_index,
        "the step order must be build -> ship -> confirm -> deploy (got "
        f"{build_index}, {transfer_index}, {landed_index}, {deploy_index}). "
        "The confirmation is what stops a truncated or silently failed "
        "transfer from reaching `compose up`",
    )
```

- [ ] **Step 5: Run the validator to verify it fails**

Run: `python deploy/trc/validate_compose.py`

Expected: FAIL. Among the reported failures:
- `jobs ['deploy'] set DOCKER_HOST at JOB level ...`
- `the 'Verify host preconditions' step drives the staging host's daemon but sets no DOCKER_HOST ...`
- `the 'Build the image on the deploy server' step shells out to docker but is in none of LOCAL_DAEMON_STEPS ...`
- `no step named 'Ship the image to the host' ...`

If it instead fails only on `check_compose_renders`, Docker is unavailable locally — see Global Constraints — and the above should still appear alongside it.

- [ ] **Step 6: Remove the job-level DOCKER_HOST**

In `.github/workflows/trc-staging-deploy.yml`, delete the whole job-level `env:` block (lines ~122-127, the comment and the `DOCKER_HOST:` line), leaving:

```yaml
jobs:
  deploy:
    name: Build and deploy paperclip to staging
    if: github.repository == 'Nature-Technologies/trc-paperclip'
    runs-on: self-hosted
    environment: staging
    timeout-minutes: 60
    steps:
```

- [ ] **Step 7: Bind DOCKER_HOST on the three existing remote steps**

Add this line to the `env:` block of `Verify host preconditions`, `Deploy`, and `Smoke test` (each already has an `env:` block; add the key, change nothing else):

```yaml
          DOCKER_HOST: ssh://${{ secrets.USERNAME }}@${{ secrets.HOST }}:${{ secrets.SSH_PORT || '22' }}
```

- [ ] **Step 8: Repoint the build at the runner**

Rename the build step and update its comments. Replace the `Set up Buildx` and `Build the image on the deploy server` steps (~lines 400-430) with:

```yaml
      - name: Set up Buildx
        uses: docker/setup-buildx-action@v4
        with:
          # The RUNNER's own daemon, and no DOCKER_HOST anywhere in scope to
          # send it elsewhere. `driver: docker` binds the build to that
          # daemon's persistent layer store, which is what keeps the cache warm
          # between dispatches now that the deploy host no longer holds it. The
          # default container driver would start empty every dispatch.
          driver: docker

      - name: Build the image on the runner
        uses: docker/build-push-action@v7
        with:
          context: .
          # Pin to the production stage explicitly: the Dockerfile declares a
          # later `cloud` stage, and without a target the default would
          # silently become that stage instead.
          target: production
          # No registry, and with `driver: docker` no `load:` either -- the
          # build writes straight into the runner's image store, which is what
          # the next step streams to the deploy host.
          push: false
          tags: |
            ${{ steps.tags.outputs.staging }}
            ${{ steps.tags.outputs.sha }}
          labels: |
            org.opencontainers.image.revision=${{ github.sha }}
          # No `platforms`: the runner and the deploy host are both linux/amd64,
          # and naming one switches on QEMU emulation. No cache-from/to: the
          # runner daemon's own layer store is the cache, and the docker driver
          # cannot use the gha cache backend anyway. No attestations: they force
          # a manifest list and an extra export pass for nothing.
          provenance: false
```

- [ ] **Step 9: Add the transfer and confirmation steps**

Insert directly after the build step, before `Deploy`:

```yaml
      - name: Ship the image to the host
        # The image is built on the runner now, so it has to reach the deploy
        # host's image store before compose can start it.
        #
        # DOCKER_HOST is bound INLINE on the `docker load` only, never as this
        # step's `env:`. A step-level binding would send `docker save` to the
        # host as well, and that failure is silent rather than loud: a
        # :git-<sha> image already present there would be saved and re-loaded,
        # deploying whatever the host was already running instead of what this
        # run built. validate_compose.py asserts the inline form.
        #
        # `docker load` over an ssh:// endpoint streams the tar to the remote
        # daemon through the Docker CLI's own SSH transport, so the host needs
        # nothing but dockerd -- no docker CLI, no zstd, no registry. Both tags
        # name the same image, so one stream carries both and the layers are
        # shared.
        #
        # Uncompressed on purpose to start with. If a dispatch shows the
        # transfer is slow, the fallback is
        #   docker save ... | zstd -T0 | ssh "$USER@$HOST" 'zstd -d | docker load'
        # which needs zstd on both machines and a docker CLI on the host.
        env:
          REMOTE_DOCKER_HOST: ssh://${{ secrets.USERNAME }}@${{ secrets.HOST }}:${{ secrets.SSH_PORT || '22' }}
          SHA_TAG: ${{ steps.tags.outputs.sha }}
          STAGING_TAG: ${{ steps.tags.outputs.staging }}
        run: |
          set -eu
          echo "shipping $SHA_TAG to the deploy host"
          docker save "$SHA_TAG" "$STAGING_TAG" | DOCKER_HOST="$REMOTE_DOCKER_HOST" docker load

      - name: Confirm the image landed on the host
        # A truncated or silently failed transfer must not reach `compose up`.
        # Without this, `up -d` would either start the PREVIOUS image or fail
        # closed on pull_policy: never -- the first of which is silent.
        env:
          DOCKER_HOST: ssh://${{ secrets.USERNAME }}@${{ secrets.HOST }}:${{ secrets.SSH_PORT || '22' }}
          SHA_TAG: ${{ steps.tags.outputs.sha }}
        run: |
          set -eu
          if ! docker image inspect "$SHA_TAG" >/dev/null 2>&1; then
            echo "::error::$SHA_TAG is not in the deploy host's image store after the transfer. Nothing has been deployed; the previous image is still running. Re-dispatch."
            exit 1
          fi
          echo "$SHA_TAG is present on the deploy host"
```

- [ ] **Step 10: Run the validator to verify it passes**

Run: `python deploy/trc/validate_compose.py`

Expected: PASS (exit 0), or only the `check_compose_renders` failure if Docker is unavailable locally.

- [ ] **Step 11: Commit**

```bash
git add .github/workflows/trc-staging-deploy.yml deploy/trc/validate_compose.py
git commit -m "$(cat <<'EOF'
ci(deploy): build paperclip on the runner and ship the image over SSH

The job-level DOCKER_HOST put every docker call, the build included, on the
staging host -- costing that box disk, CPU contention with the live
containers, and build time. Bind DOCKER_HOST per step instead, so the build
runs on the runner against its own persistent layer store, and stream the
finished image to the host with `docker save | docker load`.

No registry enters the loop, so pull_policy: never and the fail-closed
guarantee are untouched.

The transfer step binds its endpoint inline on the `docker load` only. As a
step-level env: it would send `docker save` to the host too, which fails
silently -- an image already there would be saved and re-loaded, deploying
what the host was already running.

validate_compose.py inverts with it: DOCKER_HOST goes from required at job
level to banned there and required per remote step, and every daemon-touching
step must now declare which daemon it talks to.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Move the disk guards to the runner

Cold-build headroom is now needed on the runner, not the host. This adds the preflight that answers what the runner actually is, and stops the host guard claiming it needs build space.

**Files:**
- Modify: `deploy/trc/validate_compose.py` (`LOCAL_DAEMON_STEPS`, plus a new check in `check_deploy_workflow()`)
- Modify: `.github/workflows/trc-staging-deploy.yml` (new step; `Verify host preconditions` thresholds)
- Test: `python deploy/trc/validate_compose.py`

**Interfaces:**
- Consumes: `LOCAL_DAEMON_STEPS` from Task 1.
- Produces: step name `Verify the runner can build`.

- [ ] **Step 1: Add the preflight to the local-daemon set and require it**

In `deploy/trc/validate_compose.py`, add to `LOCAL_DAEMON_STEPS`:

```python
    "Verify the runner can build",
```

Then append to `check_deploy_workflow()`:

```python
    # The build's preconditions are the RUNNER's now. This has to run before
    # the lockfile refresh, not just before the build: a runner with no local
    # daemon cannot build at all, and that must cost seconds rather than a
    # pnpm install followed by a failure with no obvious cause.
    preflight_name = "Verify the runner can build"
    preflight_step = _step_by_name(doc, preflight_name)
    check(
        preflight_step is not None,
        f"no step named {preflight_name!r} -- the build runs on the runner "
        "now, so its daemon and its free disk are preconditions of the build "
        "and have to be proven before any work starts",
    )
    if preflight_step is not None:
        preflight_lines = _script_lines(preflight_step.get("run") or "")
        check(
            any("docker version" in ln for ln in preflight_lines),
            f"the {preflight_name!r} step must run `docker version` with no "
            "DOCKER_HOST in scope -- that is what proves a LOCAL daemon exists "
            "to build against",
        )
        lockfile_index = _step_index(doc, "Refresh lockfile for Docker build context")
        preflight_index = _step_index(doc, preflight_name)
        check(
            None not in (lockfile_index, preflight_index)
            and preflight_index < lockfile_index,
            "the runner preflight must run before the lockfile refresh -- a "
            "runner that cannot build should cost seconds, not a full pnpm "
            "install first",
        )
```

- [ ] **Step 2: Run the validator to verify it fails**

Run: `python deploy/trc/validate_compose.py`

Expected: FAIL with `no step named 'Verify the runner can build' ...`

- [ ] **Step 3: Add the preflight step**

Insert into `.github/workflows/trc-staging-deploy.yml` directly after `Write SSH key and scan the host key`. It must stay after that step: the validator requires no daemon-touching step precede the SSH setup.

```yaml
      - name: Verify the runner can build
        # No DOCKER_HOST, deliberately: this asks about THIS machine. The build
        # runs here now, so the runner's daemon, cores and free disk are the
        # build's preconditions -- and a runner with no local daemon has to
        # fail here, in seconds, rather than part-way through a pnpm install.
        run: |
          set -eu
          if ! docker version --format 'local daemon {{.Server.Version}}'; then
            echo "::error::no local Docker daemon on this runner. The build runs here now, not on the staging host. Install dockerd on the runner, or point this job at a dedicated build host."
            exit 1
          fi
          echo "cores: $(nproc)"
          # The build cache lives on this machine now, so this is where cold-build
          # headroom has to exist. BuildKit's out-of-space failures name
          # everything except the cause.
          avail=$(df --output=avail -B1 /var/lib/docker 2>/dev/null | tail -1) || avail=""
          case "$avail" in
            ''|*[!0-9]*)
              echo "::warning::Could not read free space on the runner - check df -h there manually." ;;
            *)
              gb=$((avail / 1000000000))
              if [ "$avail" -lt 15000000000 ]; then
                echo "::error::Only ${gb} GB free on the runner. The build cache lives here now and a cold build needs more than that."
                exit 1
              elif [ "$avail" -lt 30000000000 ]; then
                echo "::warning::${gb} GB free on the runner - enough for a cached build, tight for a cold one."
              else
                echo "OK - ${gb} GB free on the runner."
              fi ;;
          esac
```

- [ ] **Step 4: Retune the host thresholds**

In `Verify host preconditions`, the host no longer needs build headroom — only room to receive a `docker load`. Replace the `case "$avail" in` block's three messages and the warn threshold (`25000000000` becomes `20000000000`):

```yaml
            *)
              gb=$((avail / 1000000000))
              if [ "$avail" -lt 10000000000 ]; then
                echo "::error::Only ${gb} GB free on the deploy host. Nothing builds here any more, but the incoming image load still needs room for the new image alongside the one running. See docs/deploy/trc-staging.md for what is safe to prune - note that pruning images also destroys rollback targets."
                exit 1
              elif [ "$avail" -lt 20000000000 ]; then
                echo "::warning::${gb} GB free on the deploy host - enough to receive an image, tight if it grows."
              else
                echo "OK - ${gb} GB free on the deploy host."
              fi ;;
```

Also update that step's leading comment, which still says the build happens on the host:

```yaml
      # Runs before the transfer: the host must have room to receive the image
      # and a reachable daemon. Nothing builds here any more, so this is no
      # longer a precondition of the build itself -- but a full disk still has
      # to fail before the image is streamed, not during.
```

- [ ] **Step 5: Run the validator to verify it passes**

Run: `python deploy/trc/validate_compose.py`

Expected: PASS (exit 0), modulo `check_compose_renders` if Docker is unavailable locally.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/trc-staging-deploy.yml deploy/trc/validate_compose.py
git commit -m "$(cat <<'EOF'
ci(deploy): move the cold-build disk guard to the runner

The build's preconditions are the runner's now. Add a preflight that proves a
local Docker daemon exists and guards free disk there (fail under 15 GB, warn
under 30 GB), running before the lockfile refresh so a runner that cannot
build costs seconds rather than a full pnpm install.

The host guard stays -- an incoming `docker load` still needs room -- but
stops claiming the space is for a cold build, and its warn threshold drops
from 25 GB to 20 GB.

Both sets of numbers are starting values, to be retuned once a dispatch
reports the real image size.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Bound host image growth with keep-N retention

**Files:**
- Modify: `deploy/trc/validate_compose.py` (`REMOTE_DAEMON_STEPS`, plus new checks)
- Modify: `.github/workflows/trc-staging-deploy.yml` (new final step)
- Test: `python deploy/trc/validate_compose.py`

**Interfaces:**
- Consumes: `REMOTE_DAEMON_STEPS` from Task 1; workflow-level `IMAGE` env.
- Produces: step name `Prune old images on the host`.

- [ ] **Step 1: Add the retention step to the remote set and require it**

Add to `REMOTE_DAEMON_STEPS` in `deploy/trc/validate_compose.py`:

```python
    "Prune old images on the host",
```

Then append to `check_deploy_workflow()`:

```python
    # Retention, not pruning. :git-<sha> tags live only in the deploy host's
    # image store and are the only rollback targets that exist, so `docker
    # image prune -a` there destroys every one of them. Keep-N bounds the disk
    # without that cliff. Checked against comment-stripped lines, so the
    # warnings about this command in the surrounding comments do not trip it.
    check(
        not any(
            re.search(r"image\s+prune\b.*(\s-\w*a|\s--all)", ln)
            for ln in script_lines
        ),
        "`docker image prune -a` must appear nowhere -- :git-<sha> tags are "
        "HOST-LOCAL and are the only rollback targets that exist, so it "
        "destroys all of them. Remove images by explicit keep-N instead",
    )
    retention_name = "Prune old images on the host"
    retention_step = _step_by_name(doc, retention_name)
    check(
        retention_step is not None,
        f"no step named {retention_name!r} -- the host accumulates a "
        ":git-<sha> image per deploy and nothing else removes them, which is "
        "one of the two things that filled its disk under the old scheme",
    )
    if retention_step is not None:
        check(
            retention_step.get("continue-on-error") is True,
            f"the {retention_name!r} step must set `continue-on-error: true` "
            "-- it runs after a deploy that has already succeeded and a "
            "cleanup problem must not mark that deploy red",
        )
        retention_index = _step_index(doc, retention_name)
        smoke_index = _step_index(doc, "Smoke test")
        check(
            None not in (retention_index, smoke_index)
            and smoke_index < retention_index,
            "retention must run after the smoke test -- removing images before "
            "the deploy is proven would take the rollback target with them",
        )
```

- [ ] **Step 2: Run the validator to verify it fails**

Run: `python deploy/trc/validate_compose.py`

Expected: FAIL with `no step named 'Prune old images on the host' ...`

- [ ] **Step 3: Add the retention step**

Append to the end of `.github/workflows/trc-staging-deploy.yml`, after `Smoke test`:

```yaml
      - name: Prune old images on the host
        # Deliberately NOT `docker image prune -a`: :git-<sha> tags exist only
        # in this host's image store and are the only rollback targets there
        # are, so that command destroys every one of them. Keep the newest few
        # and whatever is running; remove the rest.
        #
        # continue-on-error because the deploy has already succeeded and been
        # smoke-tested by the time this runs. A cleanup problem is a warning,
        # not a failed deploy.
        continue-on-error: true
        env:
          DOCKER_HOST: ssh://${{ secrets.USERNAME }}@${{ secrets.HOST }}:${{ secrets.SSH_PORT || '22' }}
          KEEP: "5"
        run: |
          set -eu
          # --no-trunc so the ID is the full sha256: form that `docker inspect`
          # returns below; the truncated form would never compare equal and the
          # running image would be a removal candidate on every deploy.
          running="$(docker inspect --format '{{.Image}}' paperclip 2>/dev/null || true)"
          echo "keeping the newest $KEEP :git-* images, plus the running one"
          docker images --no-trunc \
            --filter "reference=${IMAGE}:git-*" \
            --format '{{.CreatedAt}}|{{.ID}}|{{.Repository}}:{{.Tag}}' \
            | sort -r \
            | tail -n +"$((KEEP + 1))" \
            | while IFS='|' read -r _ id tag; do
                if [ -n "$running" ] && [ "$id" = "$running" ]; then
                  echo "keeping $tag (currently running)"
                  continue
                fi
                if docker rmi "$tag" >/dev/null 2>&1; then
                  echo "removed $tag"
                else
                  echo "::warning::could not remove $tag"
                fi
              done
          # Untagged leftovers only. Without -a this cannot touch a :git-<sha>.
          docker image prune -f >/dev/null 2>&1 || true
          echo "retention done"
```

- [ ] **Step 4: Run the validator to verify it passes**

Run: `python deploy/trc/validate_compose.py`

Expected: PASS (exit 0), modulo `check_compose_renders` if Docker is unavailable locally.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/trc-staging-deploy.yml deploy/trc/validate_compose.py
git commit -m "$(cat <<'EOF'
ci(deploy): bound host image growth with keep-N retention

The host gains a :git-<sha> image per deploy and nothing removed them, which
is half of what filled its disk. Keep the newest 5 plus whatever is running,
after the smoke test has proven the deploy.

Explicitly not `docker image prune -a`: those tags are host-local and are the
only rollback targets that exist, so it destroys all of them. The validator
now bans it outright.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Bound the runner's build cache

Without this the change relocates the disk problem rather than solving it.

**Files:**
- Modify: `deploy/trc/validate_compose.py` (`LOCAL_DAEMON_STEPS`, plus a new check)
- Modify: `.github/workflows/trc-staging-deploy.yml` (new final step)
- Test: `python deploy/trc/validate_compose.py`

**Interfaces:**
- Consumes: `LOCAL_DAEMON_STEPS` from Task 1; `steps.tags.outputs.*` from `Compute image tags`.
- Produces: step name `Cap the runner's build cache`.

- [ ] **Step 1: Add the cache cap to the local-daemon set and require it**

Add to `LOCAL_DAEMON_STEPS`:

```python
    "Cap the runner's build cache",
```

Then append to `check_deploy_workflow()`:

```python
    # The layer cache moved from the deploy host to the runner, and an unbounded
    # cache there just relocates the disk problem this change exists to fix.
    cache_cap_name = "Cap the runner's build cache"
    cache_cap_step = _step_by_name(doc, cache_cap_name)
    check(
        cache_cap_step is not None,
        f"no step named {cache_cap_name!r} -- the build cache lives on the "
        "runner now, and leaving it unbounded moves the disk problem instead "
        "of solving it",
    )
    if cache_cap_step is not None:
        check(
            cache_cap_step.get("continue-on-error") is True,
            f"the {cache_cap_name!r} step must set `continue-on-error: true` "
            "-- it runs after a successful deploy and must not mark it red",
        )
        cache_lines = _script_lines(cache_cap_step.get("run") or "")
        check(
            any("builder prune" in ln for ln in cache_lines),
            f"the {cache_cap_name!r} step must run `docker builder prune` with "
            "a size cap -- that is the cache this change relocated to the runner",
        )
```

- [ ] **Step 2: Run the validator to verify it fails**

Run: `python deploy/trc/validate_compose.py`

Expected: FAIL with `no step named "Cap the runner's build cache" ...`

- [ ] **Step 3: Add the cache cap step**

Append to `.github/workflows/trc-staging-deploy.yml`, after `Prune old images on the host`:

```yaml
      - name: Cap the runner's build cache
        # No DOCKER_HOST: this bounds the RUNNER's daemon. The layer cache moved
        # here from the deploy host, and leaving it unbounded would relocate the
        # disk problem rather than fix it.
        #
        # 30GB is a starting value, not a measured one. Retune it once a
        # dispatch shows how large the cache actually gets.
        continue-on-error: true
        env:
          SHA_TAG: ${{ steps.tags.outputs.sha }}
          STAGING_TAG: ${{ steps.tags.outputs.staging }}
        run: |
          set -eu
          docker builder prune --keep-storage=30GB --force
          # The image itself is not the cache -- it has already been shipped and
          # the build cache above is what keeps the next build warm -- so the
          # runner's copy can go, keeping its image store flat.
          docker image rm "$SHA_TAG" "$STAGING_TAG" >/dev/null 2>&1 || true
          echo "runner cache capped"
```

- [ ] **Step 4: Run the validator to verify it passes**

Run: `python deploy/trc/validate_compose.py`

Expected: PASS (exit 0), modulo `check_compose_renders` if Docker is unavailable locally.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/trc-staging-deploy.yml deploy/trc/validate_compose.py
git commit -m "$(cat <<'EOF'
ci(deploy): cap the build cache now that it lives on the runner

An unbounded cache on the runner would relocate the disk problem rather than
solve it. Prune to 30GB after each deploy and drop the runner's copy of the
shipped image -- the build cache, not the image, is what keeps the next build
warm.

30GB is a starting value, to be retuned once a dispatch shows the real size.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Retire the documentation describing the host-side build

Four places describe the arrangement that no longer exists. Left stale they are worse than absent — the whole point of the surrounding comments is that someone reads them before changing this file.

**Files:**
- Modify: `.github/workflows/trc-staging-deploy.yml:1-84` (header comment)
- Modify: `deploy/trc/validate_compose.py` (module docstring; two stale inline comments)
- Modify: `deploy/trc/docker-compose-staging-trc.yml:46-53` (comment)
- Modify: `docs/deploy/trc-staging.md`
- Test: `python deploy/trc/validate_compose.py`

- [ ] **Step 1: Rewrite the workflow header comment**

Replace lines 4-84 of `.github/workflows/trc-staging-deploy.yml` (everything between `run-name:` and `on:`) with:

```yaml
# Additive and TRC-only. Builds AND deploys in one run.
#
# THE BUILD RUNS ON THE RUNNER. DOCKER_HOST is bound PER STEP, never at job
# level: a job-level binding puts every docker call in the job on the deploy
# host, the build included, which is the arrangement this replaced. That cost
# the staging host disk, CPU contention with the live containers, and build
# time. validate_compose.py asserts the per-step form, and requires every
# daemon-touching step to declare which daemon it talks to.
#
# The finished image reaches the host as a stream: `docker save` on the runner
# piped into a `docker load` bound to the host INLINE. Inline is load-bearing
# -- as a step-level env: it would send the save to the host too, and that
# fails silently, re-loading whatever image is already there.
#
# There is still no registry. `push: false` leaves the image in the runner's
# store, and the compose service sets `pull_policy: never` so a missing image
# fails closed instead of reaching out to GHCR, where the retired
# publish-then-pull scheme left tags that a rollback dispatch could otherwise
# silently start.
#
# ROLLBACK IS HOST-LOCAL. `:git-<7-char-sha>` tags exist only in that host's
# image store, now bounded to the newest few by the retention step at the end.
# A `docker image prune -a` or a host rebuild destroys every rollback target.
# See docs/deploy/trc-staging.md before pruning there.
#
# Runs on a SELF-HOSTED runner: the staging host is internal and unreachable
# from a GitHub-hosted runner.
#
# Never `docker context create`/`use` here: this runner is shared with sibling
# repos (trc-hermes-agent, trc-open-webui) and `use` would repoint the DEFAULT
# daemon for their jobs too -- which would silently put the build back on the
# deploy host -- and would fail "already exists" on the second run besides.
#
# This repo carries two things neither trc-hermes-agent nor trc-open-webui
# does:
#   - a private Postgres, in the SAME compose project (see
#     deploy/trc/docker-compose-staging-trc.yml) -- paperclip and postgres
#     deliberately stay OFF trc-shared, so unlike its two siblings,
#     `trc-shared` never appears anywhere in this file
#   - a pre-existing bind mount, /srv/trc/staging/paperclip-home, that this
#     workflow does not manage the way it manages the Postgres volume: it
#     holds secrets/master.key and the only copy of the hermes_gateway
#     wiring, so outside `bootstrap: true` it is only ever verified, never
#     created. Those guards stay on plain SSH -- a DOCKER_HOST proxies the
#     Docker API, not a shell, so it cannot stat a path on the host
#     filesystem -- and run before the build even starts, so a typo'd path
#     fails in seconds rather than after a full build. Never substitute a
#     bind-mounted probe container for these checks either: Docker creates a
#     missing bind source as root, which is exactly the failure the guard
#     exists to prevent.
#
# The build needs a pnpm lockfile refresh first: the Docker build's deps stage
# installs with a frozen lockfile, so a pnpm-lock.yaml that has drifted from
# package.json fails the build rather than the app. The build is also pinned to
# the `production` target explicitly -- the Dockerfile declares a later `cloud`
# stage, and omitting `target:` would silently deploy that stage instead.
#
# Other consequences worth knowing:
#   - no env file is written anywhere. The Deploy step binds the secrets as its
#     own `env:`, and Compose resolves the compose file's ${...} references from
#     that process environment -- so no secret touches disk on the runner or the
#     server, and nothing is dotenv-parsed, which means a `$`, backtick or `#`
#     in a value is taken literally instead of interpolated or truncated. The
#     `@ : / ? #` guard on POSTGRES_PASSWORD is about DATABASE_URL rather than
#     about parsing, so it stays
#   - there is no `docker login`, so nothing here mutates the shared
#     ~/.docker/config.json
#   - the /paperclip bind mount already exists on the host and is verified, not
#     created. Its source is resolved by the REMOTE daemon, so the path must be
#     absolute
#   - disk is guarded on BOTH machines now, for different reasons: the runner
#     needs cold-build headroom, the host only needs room to receive the image
#   - the two cleanup steps at the end bound each machine's disk and are
#     `continue-on-error`: they run after a deploy that has already been
#     smoke-tested, and a cleanup problem must not mark it red
#   - the per-job ssh-agent is left RUNNING: it holds the decrypted key in
#     memory on this persistent runner until the machine reboots, one more
#     agent per dispatch. Reap them with `pkill ssh-agent` if that matters
#   - ~/.ssh/known_hosts is genuinely shared across sibling jobs on this
#     runner. The merge below is non-destructive, so the worst case is a
#     redundant rescan; see docs/deploy/trc-staging.md for the
#     one-job-at-a-time / separate-users requirement that covers it
```

- [ ] **Step 2: Update the validator's docstring and two stale comments**

In `deploy/trc/validate_compose.py`, replace the "deliberate inversions" paragraph (~lines 28-33) with:

```python
Several of the deploy-workflow checks in check_deploy_workflow() are DELIBERATE
INVERSIONS of an earlier contract. Read the reason on each check before
"restoring" any of them:

  - `--env-file` is banned rather than required, and `compose pull` is banned
    rather than required to precede `up -d` -- both from the move off GHCR and
    off a rendered env file.
  - DOCKER_HOST is banned at JOB level and required PER STEP. It was required
    at job level for exactly as long as the build ran on the deploy host; the
    build runs on the runner now, and a job-level binding would put it back.
```

Then fix the two comments that still assume a job-level binding:

The comment above the `Verify the paperclip-home guards` block (~line 990) — replace `DOCKER_HOST is job-level now, so this step inherits it -- but a DOCKER_HOST proxies` with:

```python
    # There is no job-level DOCKER_HOST for it to inherit, and it would not help
    # if there were -- a DOCKER_HOST proxies
```

And the comment above `ssh_step_index` (~line 795) — replace `Load-bearing only because DOCKER_HOST is job-level: every docker call in the job now goes over SSH` with:

```python
    # Load-bearing: every REMOTE docker call and every plain ssh in the job goes
    # over SSH
```

- [ ] **Step 3: Update the compose file comment**

In `deploy/trc/docker-compose-staging-trc.yml`, replace the comment above `pull_policy: never` (~lines 46-53) with:

```yaml
    # The deploy workflow builds this image on the RUNNER and streams it into
    # the deploy host's image store with `docker save | docker load` -- there is
    # no registry in the loop. `never` makes a missing image fail closed instead
    # of reaching out to GHCR, where the retired publish-then-pull scheme left
    # tags that a rollback dispatch could otherwise silently start. Deliberately
    # NOT set on postgres: postgres:17-alpine is an upstream image nothing here
    # builds, so it must stay fetchable.
```

- [ ] **Step 4: Update the operator documentation**

Read each section before rewriting it — the line numbers below are from the
pre-change file and the surrounding prose has to stay coherent. Each bullet
names the facts the replacement must state; the wording is yours.

In `docs/deploy/trc-staging.md`:

- Replace the "**The build runs ON the deploy host**" paragraph (~lines 16-21) with a statement that the build runs on the runner against its own persistent layer store, and that the image is streamed to the host with `docker save | docker load` over the Docker CLI's SSH transport — so the host needs only `dockerd`.
- Replace the paragraph on job-level `DOCKER_HOST` and its two consequences (~lines 42-57) with the per-step binding rule, the three-way classification (local / remote / the transfer step's inline binding), and why the transfer step's binding is inline.
- In "Runner prerequisites" (~lines 239-257), replace "The runner also needs enough free disk on the staging host, not on itself: the build happens there now" with the inverse: the runner needs its own local Docker daemon and its own free disk (fail under 15 GB, warn under 30 GB), while the host guard now only covers room to receive the image (fail under 10 GB, warn under 20 GB).
- In the rollback section (~lines 285-290), add that `:git-<sha>` images are now bounded to the newest 5 by the retention step, so the rollback window is five deploys deep rather than unbounded.
- Add a short **Migration** section recording the one-time cleanup: after the first dispatch under this scheme, run `docker builder prune -a` **on the staging host** to reclaim the cache the old arrangement left behind. Note explicitly that this is not `docker image prune -a`, which would destroy the rollback targets.

- [ ] **Step 5: Run the validator to verify nothing regressed**

Run: `python deploy/trc/validate_compose.py`

Expected: PASS (exit 0), modulo `check_compose_renders` if Docker is unavailable locally. Comments are stripped before assertions, so this proves the rewrite broke no check — including that the `docker image prune -a` ban does not trip on the comments that warn about it.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/trc-staging-deploy.yml deploy/trc/validate_compose.py deploy/trc/docker-compose-staging-trc.yml docs/deploy/trc-staging.md
git commit -m "$(cat <<'EOF'
docs(deploy): retire the documentation for the host-side build

The header comment, the operator doc, the validator docstring and the compose
file all described the build running on the deploy host. Stale here is worse
than absent: the point of these comments is that someone reads them before
changing this file.

Also records what is new -- the inline binding on the transfer step, disk
guarded on both machines for different reasons, a rollback window now five
deploys deep -- and the one-time `docker builder prune -a` on the host to
reclaim what the old scheme left behind.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## After the plan

The validator passing is necessary, not sufficient — nothing here can exercise an actual deploy. **The first `workflow_dispatch` is the acceptance test.** Watch for, and retune against:

1. `Verify the runner can build` — the daemon version, core count and free disk. If there is no local daemon, stop: the build needs a dedicated host and the preflight becomes a remote check.
2. `Ship the image to the host` — the transfer duration and image size. If it is slow, switch to the zstd fallback documented in that step's comment.
3. `Prune old images on the host` / `Cap the runner's build cache` — whether keep-5 and 30GB are the right numbers.
4. Whether the deploy's wall-clock improved, which is the point of the exercise.
