#!/usr/bin/env python3
"""Contract checks for the TRC staging deploy assets in this directory.

Additive and TRC-only: no upstream tooling reads this file.

The load-bearing assertion is `external: true` on every network and volume.
Compose prefixes a non-external named volume with the project name, so
declaring `trc-staging-paperclip-db` without `external: true` silently creates
an empty `trc-staging-paperclip_trc-staging-paperclip-db` and uses that instead
of the real volume -- Postgres then initialises an empty database and Paperclip
comes up with no instance data, without erroring. Nothing in `compose up`
output reveals it, which is why it is asserted here.

Two invariants are specific to this repo:

- `trc-shared` must NOT appear. Paperclip and Postgres deliberately stay off
  the backend bridge; only hermes-agent and open-webui join it.
- Postgres must publish no ports. It is reachable only from other containers
  on poc-net, never from the host.

`pull_policy` is asserted in BOTH directions and the asymmetry is the point:
`never` on paperclip (its image is built straight into the deploy host's image
store and never pushed, so falling back to GHCR could start a stale tag left
over from the retired publish-then-pull scheme) and NO pull_policy on postgres
(postgres:17-alpine is an upstream image nothing here builds, so `never` on it
would break the first `up -d` on a host that does not already have it).

Several of the deploy-workflow checks in check_deploy_workflow() are DELIBERATE
INVERSIONS of the contract that held while the image was built on the runner
and pushed to GHCR. DOCKER_HOST is now required at JOB level rather than banned
there, `--env-file` is banned rather than required, and `compose pull` is banned
rather than required to precede `up -d`. Read the reason on each check before
"restoring" any of them.

Run: python deploy/trc/validate_compose.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
COMPOSE = HERE / "docker-compose-staging-trc.yml"
ENV_EXAMPLE = HERE / ".env.staging.example"

EXPECTED_PROJECT = "trc-staging-paperclip"
# No trc-shared: see this module's docstring.
EXPECTED_NETWORKS = {"poc-net"}
EXPECTED_VOLUMES = {"trc-staging-paperclip-db"}
EXPECTED_CONTAINERS = {"paperclip", "paperclip-postgres"}
NO_PORTS_SERVICES = {"postgres"}
# Service-level membership, which is a different assertion from EXPECTED_NETWORKS
# above. A top-level network that no service joins is silently IGNORED, and the
# converse matters just as much here: adding `trc-shared` to either service's own
# `networks:` list would put Paperclip or Postgres on the backend bridge, which
# this project deliberately stays off, and neither `docker compose config` nor
# `up` would object.
EXPECTED_SERVICE_NETWORKS = {"paperclip": {"poc-net"}, "postgres": {"poc-net"}}

# Env keys that carry a remote daemon endpoint. `DOCKER_HOST` binds a whole
# step to the staging host; `REMOTE_DOCKER_HOST` is the transfer step's
# endpoint, bound INLINE on the `docker load` side only (see TRANSFER_STEP).
# Both must be built from secrets rather than hardcoded, so both are collected
# by _docker_host_values() -- but only the exact name `DOCKER_HOST` classifies
# a step as remote.
REMOTE_HOST_ENV_KEYS = ("DOCKER_HOST", "REMOTE_DOCKER_HOST")

# With no job-level DOCKER_HOST, every step that shells out to `docker` reaches
# one daemon or the other, and which one has to be asserted rather than
# inferred. A daemon-touching step in none of these three groups fails the
# validator, so adding a step later forces the decision.
#
# LOCAL: the runner's own daemon. Must NOT set DOCKER_HOST -- the build staying
# here is the entire point of this arrangement.
LOCAL_DAEMON_STEPS = {
    "Build the image on the runner",
    "Set up Buildx",
    "Verify the runner can build",
}
# REMOTE: the staging host's daemon. Each must set its own DOCKER_HOST.
REMOTE_DAEMON_STEPS = {
    "Confirm the image landed on the host",
    "Deploy",
    "Prune old images on the host",
    "Smoke test",
    "Verify host preconditions",
}
# Neither: `docker save` runs locally and pipes into a `docker load` that is
# bound to the host INLINE. A step-level DOCKER_HOST here would send the save to
# the host as well -- and that fails SILENTLY, because a `:git-<sha>` image
# already present there would be saved and re-loaded, deploying whatever the
# host was already running instead of what this run built.
TRANSFER_STEP = "Ship the image to the host"

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def placeholder(key: str) -> str:
    """A stand-in value that satisfies compose's own validation.

    `docker compose config` validates port specifications and image
    references, so a generic filler string would fail for `*_BIND` and
    `*_IMAGE` regardless of whether the file is correct.
    """
    if key.endswith("_IMAGE"):
        return "ghcr.io/nature-technologies/placeholder@sha256:" + "0" * 64
    if key.endswith("_BIND"):
        return "127.0.0.1"
    if key.endswith("_URL"):
        return "http://placeholder.invalid:3100"
    if key.endswith("_DIR") or key.endswith("_PATH"):
        return "/srv/trc/staging/placeholder"
    # 64 chars clears every length guard in the stack, including the gateway's
    # 16-character minimum on HERMES_API_KEY.
    return "x" * 64


def env_example_keys() -> list[str]:
    return [
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ]


def service_networks(spec: dict) -> set[str]:
    """The networks a service joins, in either the list or the mapping form.

    `networks: [poc-net]` and `networks: {poc-net: {aliases: [...]}}` are both
    valid compose and mean the same membership, so both spellings have to be
    read or a reformat could quietly turn the assertion off.
    """
    nets = (spec or {}).get("networks")
    if nets is None:
        return set()
    return set(nets)


def check_env_example_declares_every_reference() -> None:
    """Assert every ${VAR} the compose file references is declared in the example.

    `docker compose config` cannot carry this. For a plain ${VAR} substitution it
    emits a warning and still exits 0, so only the ${VAR:?} spellings would ever
    fail -- an omission from .env.staging.example would silently become a blank
    default. Comparing the two sets directly is what makes it an error.
    """
    referenced = set(
        re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", COMPOSE.read_text(encoding="utf-8"))
    )
    missing = sorted(referenced - set(env_example_keys()))
    check(
        not missing,
        f"compose references {missing} but .env.staging.example does not declare "
        "them -- a plain ${VAR} omission would otherwise default to a blank "
        "string with only a warning from `docker compose config`",
    )


def check_compose_renders() -> None:
    keys = env_example_keys()
    check(bool(keys), "no assignments found in .env.staging.example")
    if not keys:
        return
    with tempfile.NamedTemporaryFile(
        "w", suffix=".env", delete=False, encoding="utf-8"
    ) as fh:
        for key in keys:
            fh.write(f"{key}={placeholder(key)}\n")
        env_path = fh.name
    proc = subprocess.run(
        [
            "docker", "compose",
            "--env-file", env_path,
            "-f", str(COMPOSE),
            "config", "--quiet",
        ],
        capture_output=True,
        text=True,
    )
    Path(env_path).unlink(missing_ok=True)
    check(
        proc.returncode == 0,
        "`docker compose config` failed -- rendering the compose file with "
        "placeholder values for every key in .env.staging.example must "
        f"succeed, which exercises the `${{VAR:?}}` guards:\n{proc.stderr.strip()}",
    )


WORKFLOW = HERE.parents[1] / ".github" / "workflows" / "trc-staging-deploy.yml"


def _strip_inline_comment(line: str) -> str:
    """Drop a trailing shell comment, respecting quotes.

    Assertions about script behaviour cannot match the raw file text: these
    scripts document the rules they follow, so a comment saying "never set -x"
    reads as a violation and a comment saying "serialize on flock" reads as
    compliance. Only executable lines carry either meaning -- and that cuts
    both ways. A "must not appear" check tripping on a comment is merely
    noisy, but a "must appear" check being SATISFIED by a comment is unsafe:
    a comment naming `docker logout` after the real line was deleted would
    let the cleanup-step check stay green while the secret stays on the
    runner.

    A `#` inside single or double quotes is not a comment, so quote state is
    tracked rather than cutting at the first `#`.
    """
    in_single = in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            # Only a comment when it starts a word -- `foo#bar` is not one.
            if index == 0 or line[index - 1].isspace():
                return line[:index]
    return line


def _script_lines(script: str) -> list[str]:
    """Every executable line of a single shell script, comments dropped.

    Both full-line comments and inline trailing comments (see
    _strip_inline_comment) are removed, so every substring-based assertion
    built on this list sees executable text only.
    """
    lines: list[str] = []
    for ln in script.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        stripped = _strip_inline_comment(ln)
        if stripped.strip():
            lines.append(stripped)
    return lines


def _run_script_lines(doc: dict) -> list[str]:
    """Every executable line of every `run:` block in the workflow, comments dropped."""
    lines: list[str] = []
    for job in (doc.get("jobs") or {}).values():
        for step in ((job or {}).get("steps") or []):
            script = (step or {}).get("run")
            if script:
                lines += _script_lines(script)
    return lines


def _set_words(line: str) -> list[str] | None:
    """The option words of a `set ...` line, or None if the line is not one."""
    match = re.match(r"^\s*set\s+(-.*)$", line)
    return match.group(1).split() if match else None


def _enables_tracing(line: str) -> bool:
    words = _set_words(line)
    if words is None:
        return False
    for index, word in enumerate(words):
        if word == "-o":
            if index + 1 < len(words) and words[index + 1] == "xtrace":
                return True
            continue
        if word.startswith("-") and "x" in word.lstrip("-"):
            return True
    return False


def _step_by_name(doc: dict, name: str) -> dict | None:
    """The first step whose `name:` exactly matches, or None."""
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            if (step or {}).get("name") == name:
                return step
    return None


def _step_with_uses_containing(doc: dict, needle: str) -> dict | None:
    """The first step whose `uses:` value contains `needle`, or None.

    Used to scope an assertion to a single step's own `env:` mapping rather
    than the whole file -- e.g. confirming the build step specifically has no
    DOCKER_HOST, not just that DOCKER_HOST appears somewhere unrelated.
    """
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            uses = (step or {}).get("uses")
            if uses and needle in uses:
                return step
    return None


def _step_env_keys(step: dict | None) -> set[str]:
    return set((step or {}).get("env") or {})


def _strict_mode_chars(line: str) -> set[str]:
    """Short-option letters enabled by a `set ...` line, ignoring `-o name`."""
    words = _set_words(line)
    if words is None:
        return set()
    chars: set[str] = set()
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if word == "-o":
            skip = True
            continue
        if word.startswith("-"):
            chars |= set(word.lstrip("-"))
    return chars


HEREDOC_RE = re.compile(r"<<-?\s*'?[A-Za-z_][A-Za-z0-9_]*'?\s*$")

# Raw `ssh-keyscan` output must never land directly on the real
# `~/.ssh/known_hosts` -- by `>` (which truncates it) OR by piping through
# `tee` (which truncates too unless `-a` is passed, and even `tee -a` skips
# the `ssh-keygen -R` stale-entry removal the workflow relies on). `~/.ssh`
# persists between jobs on a self-hosted runner, so other entries there are
# not ours to delete or duplicate -- the scan must land in $RUNNER_TEMP first
# and be merged in with `ssh-keygen -R` plus `>>`. A literal `>` inside a
# `>>` still matches this regex, but the workflow never appends raw keyscan
# output directly (it always goes through $RUNNER_TEMP), so that combination
# does not arise here.
KEYSCAN_TRUNCATE_RE = re.compile(r"ssh-keyscan\b.*(?:>|\|\s*tee\b).*known_hosts")


def _writes_default_ssh_key(line: str) -> bool:
    """True when `line` names ~/.ssh/id_rsa, on already comment-stripped input.

    A plain substring test is safe here because _run_script_lines strips
    inline comments at the source (see _strip_inline_comment) -- a comment
    merely naming the path can no longer reach this function. A round-2
    write-indicator heuristic (requiring `>`, `tee`, `install`, etc. on the
    same line) is no longer needed and is strictly weaker: the plain test
    also catches write forms an indicator list would miss, e.g. `dd of=` or
    a heredoc redirected there.
    """
    return "~/.ssh/id_rsa" in line


def _runs_on_labels(job: dict) -> list[str]:
    """The runner labels a job requests, in all three valid spellings.

    `runs-on: self-hosted` (scalar), `runs-on: [self-hosted, linux]` (list) and
    `runs-on: {group: g, labels: [self-hosted, linux]}` (mapping) all mean the
    same thing, so all three have to be read or a reformat could quietly turn
    the assertion off.
    """
    runs_on = (job or {}).get("runs-on")
    if isinstance(runs_on, str):
        return [runs_on]
    if isinstance(runs_on, dict):
        labels = runs_on.get("labels")
        if isinstance(labels, str):
            return [labels]
        return [str(x) for x in (labels or [])]
    return [str(x) for x in (runs_on or [])]


def _environment_name(job: dict) -> str | None:
    """A job's deployment-environment name, in either spelling.

    `environment: staging` and `environment: {name: staging, url: ...}` are both
    valid and mean the same thing.
    """
    environment = (job or {}).get("environment")
    if isinstance(environment, dict):
        name = environment.get("name")
        return None if name is None else str(name)
    return None if environment is None else str(environment)


def _docker_host_values(doc: dict) -> list[tuple[str, str]]:
    """Every remote-endpoint value in the workflow, with where it was found.

    Keyed on REMOTE_HOST_ENV_KEYS, so a sibling variable that merely starts
    with `DOCKER_` is not collected here and cannot trip these assertions,
    while the transfer step's REMOTE_DOCKER_HOST is still held to the same
    built-from-secrets rule.
    """
    found: list[tuple[str, str]] = []
    for key, value in (doc.get("env") or {}).items():
        if key in REMOTE_HOST_ENV_KEYS:
            found.append(("workflow-level env", str(value)))
    for job_name, job in (doc.get("jobs") or {}).items():
        for key, value in (((job or {}).get("env")) or {}).items():
            if key in REMOTE_HOST_ENV_KEYS:
                found.append((f"job {job_name!r} env", str(value)))
        for step in (job or {}).get("steps") or []:
            for key, value in (((step or {}).get("env")) or {}).items():
                if key in REMOTE_HOST_ENV_KEYS:
                    found.append((f"step {(step or {}).get('name')!r} env", str(value)))
    return found


# A step "touches the daemon" when it shells out to `docker` or drives one of
# the docker/* actions. Under the job-level DOCKER_HOST every one of those goes
# over SSH, so all of them depend on the ssh-agent the key step sets up.
DOCKER_COMMAND_RE = re.compile(r"(^|[|&;(]\s*)docker\s")


def _step_touches_daemon(step: dict) -> bool:
    uses = str((step or {}).get("uses") or "")
    if uses.startswith("docker/"):
        return True
    return any(DOCKER_COMMAND_RE.search(ln) for ln in _script_lines((step or {}).get("run") or ""))


def _step_index(doc: dict, name: str) -> int | None:
    """The execution position of the first step with this exact `name:`."""
    return next(
        (
            index for index, step in _steps_with_index(doc)
            if (step or {}).get("name") == name
        ),
        None,
    )


def _build_step_index(doc: dict) -> int | None:
    return next(
        (
            index for index, step in _steps_with_index(doc)
            if "docker/build-push-action" in str((step or {}).get("uses") or "")
        ),
        None,
    )


def _steps_with_index(doc: dict) -> list[tuple[int, dict]]:
    """Every step in the workflow, numbered in the order it executes.

    Numbering restarts per job, which is correct: ordering assertions are only
    meaningful within one job's sequential step list.
    """
    numbered: list[tuple[int, dict]] = []
    for job in (doc.get("jobs") or {}).values():
        for index, step in enumerate((job or {}).get("steps") or []):
            numbered.append((index, step or {}))
    return numbered


def _docker_exec_is_interactive(line: str) -> bool:
    """True when the `docker exec` on this line passes an -i style flag.

    Flags are the dash-prefixed tokens between `docker exec` and the container
    name, so `-i`, `-it` and `-ti` all count.
    """
    after = line.split("docker exec", 1)[1].split()
    for token in after:
        if not token.startswith("-"):
            break
        if "i" in token.lstrip("-"):
            return True
    return False


def check_deploy_workflow() -> None:
    """Assert the deploy workflow's security and reproducibility invariants.

    These are properties a generic YAML linter cannot know about: that the
    BUILD and the deploy both run against the staging host's daemon via a
    job-level DOCKER_HOST (never a persistent `docker context`, never at
    workflow level, and never overridden per step), that buildx binds to that
    daemon's own persistent layer store, that no registry is in the loop at
    all, that no env file is rendered so every compose variable comes from the
    Deploy step's own `env:`, that the external volume and the paperclip-home
    bind-mount source are verified rather than unconditionally pre-created,
    and the ways this workflow could otherwise leak or weaken credentials.

    Several of these are DELIBERATE INVERSIONS of the Phase 1 contract, where
    the image was built on the runner and pushed to GHCR: DOCKER_HOST was
    step-scoped, `--env-file` was mandatory and `compose pull` had to precede
    `up -d`. Each is now asserted the other way round, with the reason on the
    check itself.
    """
    check(WORKFLOW.is_file(), f"missing {WORKFLOW}")
    if not WORKFLOW.is_file():
        return
    raw = WORKFLOW.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)

    # YAML 1.1 parses the bare key `on` as the boolean True, so read both
    # spellings rather than guessing which one PyYAML lands on.
    triggers = doc.get("on", doc.get(True)) or {}
    check(
        set(triggers) == {"workflow_dispatch"},
        "deploy must be workflow_dispatch only -- auto-deploy was explicitly "
        f"rejected; got triggers {sorted(str(t) for t in triggers)}",
    )
    inputs = ((triggers.get("workflow_dispatch") or {}).get("inputs")) or {}
    check(
        "bootstrap" in inputs,
        "workflow_dispatch must take a `bootstrap` input -- it gates both the "
        "external-volume creation and the paperclip-home guard's "
        "instance-state/creation behaviour",
    )
    check(
        (inputs.get("bootstrap") or {}).get("type") == "boolean",
        "`bootstrap` must be typed `boolean` -- the guard steps rely on it "
        "rendering as exactly `true` or `false`",
    )
    check(
        (inputs.get("bootstrap") or {}).get("default") is False,
        "`bootstrap` must default to `false` -- an empty paperclip-home or a "
        "missing volume must be rejected unless a deploy explicitly opts in",
    )
    # The plan's three non-negotiables, none of which was gated before: a
    # mutation test proved `runs-on: ubuntu-latest` + `environment: Staging` +
    # a hardcoded `DOCKER_HOST: ssh://root@10.0.0.9:22` all passed together.
    # The first two fail loudly at runtime; the hardcoded host is the SILENT
    # one -- it would render every application secret and deploy them to
    # whatever machine that literal names.
    for job_name, job in (doc.get("jobs") or {}).items():
        labels = _runs_on_labels(job)
        check(
            "self-hosted" in labels,
            f"job {job_name!r} must request the `self-hosted` runner label "
            f"(got runs-on {labels!r}) -- the staging host is internal and "
            "unreachable from a GitHub-hosted runner, so `ubuntu-latest` "
            "fails at the first `ssh-keyscan` after the secrets have already "
            "been rendered",
        )
        env_name = _environment_name(job)
        check(
            env_name == "staging",
            f"job {job_name!r} must set `environment: staging`, exactly and in "
            f"lowercase (got {env_name!r}) -- GitHub matches environment names "
            "CASE-SENSITIVELY, so `Staging` resolves no secrets at all and "
            "every one of them arrives as the empty string",
        )

    docker_hosts = _docker_host_values(doc)
    check(
        bool(docker_hosts),
        "no DOCKER_HOST is set anywhere -- the deploy would run every "
        "docker/compose call against the runner's own daemon",
    )
    for where, value in docker_hosts:
        check(
            "secrets.HOST" in value and "secrets.USERNAME" in value,
            f"the DOCKER_HOST on {where} is {value!r}, which does not "
            "reference both `secrets.HOST` and `secrets.USERNAME`. Every "
            "DOCKER_HOST must be built from those secrets so a literal host "
            "cannot be substituted: a hardcoded value is the one failure in "
            "this file that is SILENT -- it renders every application secret "
            "and deploys them to whatever machine that literal names, with "
            "the smoke tests passing against it",
        )

    script_lines = _run_script_lines(doc)

    traced = [ln.strip() for ln in script_lines if _enables_tracing(ln)]
    check(
        not traced,
        f"shell tracing is enabled by {traced} -- tracing prints every secret "
        "into the run log. This catches `set -x`, `set -eux`, `set -xe`, "
        "`set -e -u -x` and `set -o xtrace`, while leaving `set -o pipefail` "
        "alone",
    )
    check(
        any({"e", "u"} <= _strict_mode_chars(ln) for ln in script_lines),
        "no `run:` block enables strict mode -- at least one must set both -e "
        "and -u",
    )
    check(
        not any(
            "StrictHostKeyChecking=no" in ln or "StrictHostKeyChecking no" in ln
            for ln in script_lines
        ),
        "StrictHostKeyChecking must never be disabled -- host keys are "
        "scanned at deploy time with `ssh-keyscan` (trust-on-first-use) "
        "rather than pinned in a secret, so this is the only thing standing "
        "between a mid-run key change and a silently accepted new key",
    )

    # Two independent things are banned UNLESS reachable only through a branch
    # that tests the `bootstrap` input: `docker volume create` (the external
    # Postgres volume) and `mkdir` of the paperclip-home bind-mount source.
    # Phase 1c's `bootstrap` input creates both, loudly, on a fresh host. What
    # must never happen is a SILENT, unconditional creation of either: for the
    # volume that converts Compose's fail-closed behaviour on a missing
    # external volume into a silently EMPTY one; for paperclip-home it lets a
    # typo'd path be silently accepted as a fresh install instead of failing
    # loudly. Checked per-step (each `run:` is one contiguous script, and the
    # paperclip-home guard's script also travels over `ssh ... <<'REMOTE'`,
    # which is still literally part of the step's `run:` text) rather than on
    # the flattened cross-job line list, so "bootstrap" merely appearing
    # somewhere else in the file cannot gate a create in an unrelated step.
    #
    # A depth-tracked if/elif/else/fi scan, not "does 'bootstrap' appear
    # anywhere earlier in the step": that weaker check passes an
    # unconditional create placed AFTER the bootstrap branch's `fi` (the
    # branch closed, so it no longer gates anything below it), which is
    # exactly the fail-open case a reviewer found. Each stack frame tracks
    # whether the CURRENTLY ACTIVE clause of that if-block (the most recent
    # if/elif/else at that depth) tests `bootstrap`; a create only passes
    # while at least one enclosing frame is in its bootstrap-gated clause.
    mkdir_paperclip_home_re = re.compile(
        r"\bmkdir\b[^\n]*(paperclip-home|\$\{?PAPERCLIP_HOME_DIR\}?)"
    )
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            script = (step or {}).get("run") or ""
            if not script:
                continue
            step_lines = _script_lines(script)
            stack: list[bool] = []
            for ln in step_lines:
                stripped = ln.strip()
                m = re.match(r"^(if|elif)\b(.*)$", stripped)
                if m:
                    # Case-insensitive: all three repos now read the input
                    # through an `env: BOOTSTRAP:` binding and test
                    # `[ "$BOOTSTRAP" = "true" ]`, rather than splicing
                    # `${{ inputs.bootstrap }}` into the shell text.
                    gated = "bootstrap" in stripped.lower()
                    if m.group(1) == "elif" and stack:
                        stack[-1] = gated
                    else:
                        stack.append(gated)
                    continue
                if re.match(r"^else\b", stripped):
                    if stack:
                        stack[-1] = False
                    continue
                if re.match(r"^fi\b", stripped):
                    if stack:
                        stack.pop()
                    continue
                if re.search(r"\bdocker\s+volume\s+create\b", ln):
                    check(
                        any(stack),
                        f"{ln.strip()!r} pre-creates a volume on the host "
                        "reachable OUTSIDE a branch gated on the `bootstrap` "
                        "input (either never inside one, or after that "
                        "branch's `fi` already closed it). Compose REFUSES "
                        "to start when an external volume is missing, and "
                        "that fail-closed behaviour is the entire point of "
                        "declaring the volume external: an unconditional "
                        "create silently turns a loud failure into an EMPTY "
                        "volume and the run goes green -- Postgres "
                        "initialises a brand-new database, Paperclip comes "
                        "up with no data, and every smoke test still "
                        "passes. `docker network create poc-net` is fine "
                        "and stays unconditional: a network carries no "
                        "data",
                    )
                if mkdir_paperclip_home_re.search(ln):
                    check(
                        any(stack),
                        f"{ln.strip()!r} creates the /paperclip bind-mount "
                        "source reachable OUTSIDE a branch gated on the "
                        "`bootstrap` input (either never inside one, or "
                        "after that branch's `fi` already closed it). That "
                        "directory holds secrets/master.key and the only "
                        "copy of the hermes_gateway wiring, so a typo'd path "
                        "must fail loudly instead of being silently accepted "
                        "as a fresh install -- outside `bootstrap: true` "
                        "this must never be reachable",
                    )

    check(
        any("docker volume inspect trc-staging-paperclip-db" in ln for ln in script_lines),
        "the deploy must verify the external volume with `docker volume inspect` "
        "and refuse to run if it is absent -- Compose fails closed on a missing "
        "external volume, and pre-creating one would boot Postgres against a "
        "silently empty volume with every smoke test still passing",
    )
    check(
        not any("docker context create" in ln for ln in script_lines),
        "`docker context create` must appear nowhere -- a context is "
        "persistent state on a self-hosted runner: `create` fails 'already "
        "exists' on the second run, `use` repoints the runner's default "
        "daemon for every later job, and buildx binds to whichever daemon is "
        "current, so an active context would build the image on the deploy "
        "host instead of the runner. Use step-scoped DOCKER_HOST instead",
    )
    # Inverted in Phase 2, and the inversion is the point: there is no env file
    # any more. The Deploy step binds every secret as its own `env:` and
    # Compose resolves the compose file's ${...} references straight out of
    # that process environment, so no secret is written to disk on the runner
    # or the server, and nothing is dotenv-parsed -- which is what lets a `$`,
    # a backtick or a `#` inside a secret survive verbatim instead of being
    # interpolated or truncated. Re-introducing `--env-file` would silently put
    # both properties back the way they were.
    env_file_uses = [ln.strip() for ln in script_lines if "--env-file" in ln]
    check(
        not env_file_uses,
        f"{env_file_uses} invokes compose with `--env-file` -- there is no env "
        "file any more. Secrets reach Compose as the Deploy step's own `env:` "
        "and are resolved from the process environment, so nothing touches "
        "disk and nothing is dotenv-parsed. An env file would reinstate both "
        "the on-disk secret and the interpolation/truncation hazard that the "
        "retired charset guards existed to work around",
    )
    env_file_writes = [
        ln.strip() for ln in script_lines
        if re.search(r"(>>?\s*|\brm\b[^\n]*)[\"']?\.?[\w./-]*\.env(\.\w+)?\b", ln)
    ]
    check(
        not env_file_writes,
        f"{env_file_writes} writes or cleans up an env file -- no secret may be "
        "rendered to disk on this persistent runner. If an env file is ever "
        "needed again, the `if: always()` cleanup step this workflow no longer "
        "has must come back with it",
    )
    # `compose pull` is now WRONG, not merely redundant. The image is built
    # straight into the deploy host's image store and never leaves it, so
    # there is no registry to pull from -- but GHCR still holds the tags the
    # retired publish-then-pull scheme pushed, so a `pull` would quietly fetch
    # and run one of those stale images instead of what this run built.
    pulls = [ln.strip() for ln in script_lines if "compose" in ln and " pull" in ln]
    check(
        not pulls,
        f"{pulls} runs `compose pull` -- the build now puts the image directly "
        "into the deploy host's image store and there is no registry in the "
        "loop. GHCR still carries tags from the retired publish-then-pull "
        "scheme, so a pull would silently replace what this run built with a "
        "stale image. The compose service's `pull_policy: never` is the other "
        "half of that guard",
    )
    check(
        any("compose" in ln and "up -d" in ln for ln in script_lines),
        "no `compose up -d` -- the deploy has to actually roll the stack over",
    )
    # There is no other source for these values now, so an omission here is a
    # `${VAR:?}` failure at deploy time (or, for the plain `${VAR}` spellings,
    # a silently blank value).
    deploy_step = _step_by_name(doc, "Deploy")
    check(
        deploy_step is not None,
        "no step named 'Deploy' -- it is the step that binds every compose "
        "variable as its own `env:`, which is the only source for them now "
        "that no env file is written",
    )
    if deploy_step is not None:
        referenced = set(
            re.findall(
                r"\$\{([A-Za-z_][A-Za-z0-9_]*)", COMPOSE.read_text(encoding="utf-8")
            )
        )
        missing = sorted(referenced - _step_env_keys(deploy_step))
        check(
            not missing,
            f"the 'Deploy' step's `env:` does not declare {missing}, which the "
            "compose file references. With no `--env-file` its own environment "
            "is the ONLY source Compose can resolve them from: a `${VAR:?}` "
            "reference fails the deploy outright, and a plain `${VAR}` one "
            "renders as an empty string with nothing erroring",
        )

    # The defect class that has cost this project the most, shipped twice in
    # Phase 1. A `docker exec` fed a heredoc WITHOUT -i gets no stdin, so the
    # body never runs and the step exits 0 -- a smoke test that silently
    # tests nothing. The inverse bites too: -i on a call that is not
    # heredoc-fed makes it swallow the enclosing script's remaining lines.
    # This repo's Postgres check is the deliberate exception PROVING the
    # rule the other way: `docker exec -e PGPASSWORD paperclip-postgres
    # psql ...` is not heredoc-fed and correctly carries no -i.
    # `_run_script_lines` drops full-line comments, so prose mentioning
    # `docker exec` cannot trip this.
    for ln in script_lines:
        if "docker exec" not in ln:
            continue
        fed = bool(HEREDOC_RE.search(ln))
        interactive = _docker_exec_is_interactive(ln)
        check(
            fed == interactive,
            f"`docker exec` mismatch on: {ln.strip()!r} -- a heredoc-fed call "
            "MUST pass -i or no stdin reaches the container, the body never "
            "runs and the step exits 0; a call that is NOT heredoc-fed must "
            "NOT pass -i, or it consumes the rest of the enclosing script",
        )

    # INVERTED IN PHASE 2, and the inversion is the whole point of the change:
    # Inverted: DOCKER_HOST was REQUIRED here while the build ran on the deploy
    # host. It is banned now for exactly the same reason it was required then --
    # a job-level binding puts every docker call in the job, the build included,
    # on the staging host, and moving the build off that machine is the entire
    # point. Workflow level stays banned as it always was.
    job_level_docker_host = [
        name for name, job in (doc.get("jobs") or {}).items()
        if "DOCKER_HOST" in set(((job or {}).get("env")) or {})
    ]
    check(
        not job_level_docker_host,
        f"jobs {job_level_docker_host} set DOCKER_HOST at JOB level -- that "
        "puts every docker call in the job on the staging host, the build "
        "included, which is the arrangement this replaced. The build has to run "
        "on the runner, so bind DOCKER_HOST per step instead",
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
                f"the {step_name!r} step sets DOCKER_HOST -- it must run against "
                "the RUNNER's own daemon. Moving the build off the staging host "
                "is what this workflow exists to do",
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

    # Load-bearing only because DOCKER_HOST is job-level: every docker call in
    # the job now goes over SSH, authenticated by the agent the key step sets
    # up. A docker call before it fails on host-key verification -- or worse,
    # on a runner whose $HOME already trusts the host, silently uses whatever
    # default identity happens to be lying around.
    ssh_step_index: int | None = None
    for index, step in _steps_with_index(doc):
        if (step or {}).get("name") == "Write SSH key and scan the host key":
            ssh_step_index = index
            break
    if ssh_step_index is not None:
        too_early = [
            (step or {}).get("name")
            for index, step in _steps_with_index(doc)
            if index < ssh_step_index and _step_touches_daemon(step)
        ]
        check(
            not too_early,
            f"steps {too_early} run a docker command (or a docker/* action) "
            "BEFORE 'Write SSH key and scan the host key'. Every docker call "
            "in this job goes to the deploy host over SSH via the job-level "
            "DOCKER_HOST, so the key must be loaded into the ssh-agent and the "
            "host key scanned before the first one of them",
        )

    # The build now runs ON the deploy host and pushes nowhere.
    build_step = _step_with_uses_containing(doc, "docker/build-push-action")
    check(
        build_step is not None,
        "no step uses docker/build-push-action -- build and deploy are "
        "unified in this workflow, so the image must be built here",
    )
    if build_step is not None:
        build_with = build_step.get("with") or {}
        check(
            build_with.get("push") is False,
            "the docker/build-push-action step must set `push: false` -- there "
            "is no registry in the loop any more. The build writes straight "
            "into the deploy host's image store, which is the same daemon "
            "`compose up -d` talks to, and nothing logs in to GHCR",
        )
        check(
            build_with.get("target") == "production",
            "the docker/build-push-action step must pin `target: production` "
            "-- the Dockerfile declares a later `cloud` stage, and omitting "
            "`target:` would silently deploy that stage instead",
        )
    # The driver is the entire performance argument for building on the host.
    buildx_step = _step_with_uses_containing(doc, "docker/setup-buildx-action")
    check(
        buildx_step is not None,
        "no step uses docker/setup-buildx-action",
    )
    if buildx_step is not None:
        check(
            (buildx_step.get("with") or {}).get("driver") == "docker",
            "the docker/setup-buildx-action step must set `driver: docker` -- "
            "that binds the build to the remote daemon's OWN builder and its "
            "persistent layer store. The default `docker-container` driver "
            "creates a fresh builder with an EMPTY cache on every dispatch, "
            "which is exactly the cold 15-45 minute build this arrangement "
            "exists to avoid",
        )

    # Nothing authenticates to a registry any more, which is what makes the
    # per-job DOCKER_CONFIG (and the `docker logout` that needed it) removable.
    # A login would start mutating the shared ~/.docker/config.json again, out
    # from under a sibling repo's concurrent deploy on this same $HOME.
    logins = [ln.strip() for ln in script_lines if re.search(r"\bdocker\s+log(in|out)\b", ln)]
    check(
        not logins,
        f"{logins} runs `docker login`/`docker logout` -- nothing is pushed or "
        "pulled from a registry now, and this runner shares ~/.docker/config."
        "json with sibling repos' concurrent deploys. A login here would "
        "mutate that shared file, and the logout that has to follow it would "
        "strip a sibling job's credential mid-deploy",
    )
    check(
        _step_with_uses_containing(doc, "docker/login-action") is None,
        "no step may use docker/login-action -- see the `docker login` check "
        "above; nothing in this workflow talks to a registry any more",
    )

    keyscan_truncates = [
        ln.strip() for ln in script_lines
        if KEYSCAN_TRUNCATE_RE.search(ln) and "RUNNER_TEMP" not in ln
    ]
    check(
        not keyscan_truncates,
        f"{keyscan_truncates} writes `ssh-keyscan` output directly onto "
        "known_hosts (via `>` or piped through `tee`), which either "
        "TRUNCATES the file or skips the `ssh-keygen -R` stale-entry removal "
        "-- ~/.ssh/known_hosts persists between jobs on a self-hosted "
        "runner, so entries already there are not ours to delete or "
        "duplicate. Scan into $RUNNER_TEMP first, remove any stale entry for "
        "this host with `ssh-keygen -R`, then append (`>>`)",
    )

    # The negative guard above is NEGATIVE ONLY, which makes it much weaker
    # than it reads: deleting both `ssh-keygen -R` lines AND truncating the
    # append leaves nothing for KEYSCAN_TRUNCATE_RE to match, so the file
    # passes with no host-key handling at all. These four positive assertions
    # are what actually require the non-destructive merge to exist, and they
    # are scoped to the SSH-setup step by name rather than to the flattened
    # cross-step line list, so a stray `>>` somewhere else cannot satisfy them.
    ssh_step_name = "Write SSH key and scan the host key"
    ssh_step = _step_by_name(doc, ssh_step_name)
    check(
        ssh_step is not None,
        f"no step named {ssh_step_name!r} -- the deploy must scan the host key "
        "into $RUNNER_TEMP and merge it into ~/.ssh/known_hosts before any "
        "ssh/scp/DOCKER_HOST call",
    )
    if ssh_step is not None:
        ssh_lines = _script_lines(ssh_step.get("run") or "")
        check(
            any("ssh-keyscan" in ln and "RUNNER_TEMP" in ln for ln in ssh_lines),
            f"the {ssh_step_name!r} step must run `ssh-keyscan` into a file "
            "under $RUNNER_TEMP -- the scan has to land in job-scoped scratch "
            "space first so the merge into the shared ~/.ssh/known_hosts can "
            "be non-destructive",
        )
        check(
            any(
                re.search(r"\btest\s+-s\b", ln) and "RUNNER_TEMP" in ln
                for ln in ssh_lines
            ),
            f"the {ssh_step_name!r} step must `test -s` the scanned file under "
            "$RUNNER_TEMP -- `ssh-keyscan` exits 0 even when nothing answered, "
            "so without this the run continues with an EMPTY known_hosts and "
            "fails much later, after the build, on a confusing host-key error",
        )
        keygen_removals = [
            ln for ln in ssh_lines if re.search(r"\bssh-keygen\s+-R\b", ln)
        ]
        bracketed = [ln for ln in keygen_removals if "[" in ln]
        bare = [ln for ln in keygen_removals if "[" not in ln]
        check(
            len(keygen_removals) >= 2 and bool(bracketed) and bool(bare),
            f"the {ssh_step_name!r} step must call `ssh-keygen -R` TWICE, once "
            "for the bare host and once for the `[host]:port` spelling (found "
            f"{len(keygen_removals)}: {len(bare)} bare, {len(bracketed)} "
            "bracketed) -- `ssh-keyscan` writes a bare host for port 22 and "
            "`[host]:port` otherwise, so removing only one spelling leaves a "
            "stale key that makes StrictHostKeyChecking abort the deploy after "
            "a host rebuild",
        )
        check(
            any(
                ">>" in ln and "known_hosts" in ln and "~/.ssh" in ln
                for ln in ssh_lines
            ),
            f"the {ssh_step_name!r} step must APPEND (`>>`) the scanned key "
            "onto ~/.ssh/known_hosts -- without the append the scan never "
            "reaches the file ssh actually reads, and with `>` instead it "
            "would truncate entries sibling jobs on this persistent runner "
            "rely on",
        )

    # The private key must never land in ~/.ssh -- this runner is shared
    # with sibling repos' deploys (trc-hermes-agent, trc-open-webui), which
    # can run concurrently on the same $HOME. A shared ~/.ssh/id_rsa would
    # let one job's `rm -f ~/.ssh/id_rsa` cleanup delete the key a sibling
    # job is mid-deploy with. It must live only under $RUNNER_TEMP, loaded
    # into a per-job ssh-agent.
    check(
        not any(_writes_default_ssh_key(ln) for ln in script_lines),
        "the private key must never be written to ~/.ssh/id_rsa -- this "
        "runner is shared with sibling jobs and reused across them, so a "
        "shared key file lets one job's cleanup delete the key a sibling is "
        "mid-deploy with. Write it under $RUNNER_TEMP and load it into a "
        "per-job ssh-agent instead",
    )

    # The `if: always()` cleanup step is GONE in Phase 2, matching
    # trc-hermes-agent and trc-open-webui, and the checks above are what make
    # its removal safe rather than an oversight: no env file is rendered and
    # nothing logs in to a registry, so the private key is the only secret that
    # still reaches the runner's disk -- and it lives under $RUNNER_TEMP, which
    # the runner clears at the start of every job. This assertion pins that
    # reasoning down: if a step ever writes the key outside $RUNNER_TEMP, the
    # cleanup step has to come back with it.
    key_writes_outside_runner_temp = [
        ln.strip() for ln in script_lines
        if "id_rsa" in ln and "RUNNER_TEMP" not in ln
    ]
    check(
        not key_writes_outside_runner_temp,
        f"{key_writes_outside_runner_temp} names id_rsa outside $RUNNER_TEMP. "
        "There is no `if: always()` cleanup step any more -- that is safe only "
        "because the key never leaves $RUNNER_TEMP, which the runner clears at "
        "the start of each job. A key written anywhere else on this persistent "
        "runner would survive the run, so it would need an explicit cleanup",
    )

    # Repo-specific: the paperclip-home guards must run on plain SSH. DOCKER_HOST
    # is job-level now, so this step inherits it -- but a DOCKER_HOST proxies the
    # Docker API, not a shell, so it can never stat a path on the host
    # filesystem. The guard has to reach the host over its own ssh.
    guard_step = _step_by_name(doc, "Verify the paperclip-home guards")
    check(
        guard_step is not None,
        "no step named 'Verify the paperclip-home guards' -- the deploy must "
        "verify /srv/trc/staging/paperclip-home before the build starts",
    )
    if guard_step is not None:
        guard_lines = _script_lines(guard_step.get("run") or "")
        guard_script = "\n".join(guard_lines)
        check(
            any(re.search(r"(^|[|&;(]\s*)ssh\s", ln) for ln in guard_lines),
            "the paperclip-home guard must reach the host over plain `ssh` -- "
            "it stats a path on the host FILESYSTEM, and the job-level "
            "DOCKER_HOST it inherits is an API proxy, not a shell, so no "
            "docker call can carry this check. Never substitute a bind-mounted "
            "probe container either: Docker creates a missing bind source as "
            "root, which is the exact failure this guard exists to prevent",
        )
        check(
            "stat -Lc" in guard_script,
            "the paperclip-home guard must read ownership with `stat -Lc` "
            "(the -L dereferences symlinks, since both `[ -d ]` and Docker's "
            "bind mount follow them)",
        )
        guard_index = _step_index(doc, "Verify the paperclip-home guards")
        build_index = _build_step_index(doc)
        check(
            guard_index is not None
            and build_index is not None
            and guard_index < build_index,
            "the paperclip-home guard must run BEFORE the build step -- the "
            "build happens on the deploy host and can take 45 minutes cold, so "
            "a typo'd path or a wrong owner has to fail in seconds rather than "
            "after the whole build",
        )

    # The two value guards that SURVIVED the move off the rendered env file.
    # The generic `$`/backtick/`#` charset guard is gone with the dotenv parsing
    # that made it necessary; these two are about what the value MEANS, so
    # dropping them silently would leave the deploy shipping a broken stack.
    # Both must precede the build for the same reason the host preconditions
    # do: neither can produce a working deploy, and finding that out after a
    # cold build on the host wastes the entire run.
    secrets_step_name = "Verify the application secrets"
    secrets_step = _step_by_name(doc, secrets_step_name)
    check(
        secrets_step is not None,
        f"no step named {secrets_step_name!r} -- POSTGRES_PASSWORD and "
        "PAPERCLIP_PUBLIC_URL must still be validated before the build",
    )
    if secrets_step is not None:
        secrets_script = "\n".join(_script_lines(secrets_step.get("run") or ""))
        check(
            "[@:/?#]" in secrets_script,
            f"the {secrets_step_name!r} step must reject a POSTGRES_PASSWORD "
            "containing @ : / ? or #. This guard is NOT about parsing (nothing "
            "is dotenv-parsed any more) -- the compose file interpolates the "
            "value into `postgres://paperclip:${POSTGRES_PASSWORD}@postgres:"
            "5432/paperclip`, where those characters silently corrupt the "
            "connection string with no error from Compose",
        )
        check(
            "localhost" in secrets_script and "127.0.0.1" in secrets_script,
            f"the {secrets_step_name!r} step must reject a "
            "PAPERCLIP_PUBLIC_URL that is empty or points at "
            "localhost/127.0.0.1 -- it is baked into auth callbacks and shown "
            "to the first admin, so a loopback value produces an instance "
            "nobody outside the host can claim",
        )
        secrets_index = _step_index(doc, secrets_step_name)
        build_index = _build_step_index(doc)
        check(
            secrets_index is not None
            and build_index is not None
            and secrets_index < build_index,
            f"the {secrets_step_name!r} step must run BEFORE the build -- a "
            "secret this shape cannot produce a working deploy, and finding "
            "that out after a cold build wastes the whole run",
        )

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
        "start it. Without this step `up -d` finds no image and "
        "`pull_policy: never` fails it closed",
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
            "INLINE DOCKER_HOST= binding. Inline is what keeps `docker save` on "
            "the runner while the load goes to the host",
        )

    build_index = _build_step_index(doc)
    transfer_index = _step_index(doc, TRANSFER_STEP)
    landed_index = _step_index(doc, "Confirm the image landed on the host")
    deploy_index = _step_index(doc, "Deploy")
    check(
        None not in (build_index, transfer_index, landed_index, deploy_index)
        and build_index < transfer_index < landed_index < deploy_index,
        "the step order must be build -> ship -> confirm -> deploy (got "
        f"{build_index}, {transfer_index}, {landed_index}, {deploy_index}). The "
        "confirmation is what stops a truncated or silently failed transfer "
        "from reaching `compose up`",
    )

    # The build's preconditions are the RUNNER's now. This has to run before the
    # lockfile refresh, not just before the build: a runner with no local daemon
    # cannot build at all, and that must cost seconds rather than a pnpm install
    # followed by a failure with no obvious cause.
    preflight_name = "Verify the runner can build"
    preflight_step = _step_by_name(doc, preflight_name)
    check(
        preflight_step is not None,
        f"no step named {preflight_name!r} -- the build runs on the runner now, "
        "so its daemon and its free disk are preconditions of the build and "
        "have to be proven before any work starts",
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

    # Retention, not pruning. :git-<sha> tags live only in the deploy host's
    # image store and are the only rollback targets that exist, so `docker image
    # prune -a` there destroys every one of them. Keep-N bounds the disk without
    # that cliff. Checked against comment-stripped lines, so the warnings about
    # this command in the surrounding comments do not trip it.
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
        f"no step named {retention_name!r} -- the host accumulates a :git-<sha> "
        "image per deploy and nothing else removes them, which is one of the "
        "two things that filled its disk under the old scheme",
    )
    if retention_step is not None:
        check(
            retention_step.get("continue-on-error") is True,
            f"the {retention_name!r} step must set `continue-on-error: true` -- "
            "it runs after a deploy that has already succeeded and a cleanup "
            "problem must not mark that deploy red",
        )
        retention_index = _step_index(doc, retention_name)
        smoke_index = _step_index(doc, "Smoke test")
        check(
            None not in (retention_index, smoke_index)
            and smoke_index < retention_index,
            "retention must run after the smoke test -- removing images before "
            "the deploy is proven would take the rollback target with them",
        )


def report() -> int:
    if failures:
        print(f"{len(failures)} check(s) failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("all TRC deploy checks passed")
    return 0


def main() -> int:
    check(COMPOSE.is_file(), f"missing {COMPOSE}")
    check(ENV_EXAMPLE.is_file(), f"missing {ENV_EXAMPLE}")
    if failures:
        return report()

    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    check(
        doc.get("name") == EXPECTED_PROJECT,
        f"top-level `name:` must be {EXPECTED_PROJECT!r}, got {doc.get('name')!r}",
    )

    networks = doc.get("networks") or {}
    check(
        set(networks) == EXPECTED_NETWORKS,
        f"networks must be exactly {sorted(EXPECTED_NETWORKS)} -- paperclip and "
        f"postgres deliberately stay off trc-shared; got {sorted(networks)}",
    )
    for name, spec in networks.items():
        check(
            bool((spec or {}).get("external")),
            f"network {name!r} must declare `external: true` -- no single "
            "compose project owns creation of a shared bridge",
        )

    volumes = doc.get("volumes") or {}
    check(
        set(volumes) == EXPECTED_VOLUMES,
        f"volumes must be exactly {sorted(EXPECTED_VOLUMES)}, got {sorted(volumes)}",
    )
    for name, spec in volumes.items():
        check(
            bool((spec or {}).get("external")),
            f"volume {name!r} must declare `external: true` -- see this "
            "module's docstring for the silent-data-loss failure this prevents",
        )

    services = doc.get("services") or {}
    container_names = {spec.get("container_name") for spec in services.values()}
    check(
        container_names == EXPECTED_CONTAINERS,
        f"container_name set must be exactly {sorted(EXPECTED_CONTAINERS)}, "
        f"got {sorted(n for n in container_names if n)}",
    )

    check(
        set(services) == set(EXPECTED_SERVICE_NETWORKS),
        f"services must be exactly {sorted(EXPECTED_SERVICE_NETWORKS)}, got "
        f"{sorted(services)} -- every service needs an entry in "
        "EXPECTED_SERVICE_NETWORKS or its network membership goes unasserted",
    )

    for svc, spec in services.items():
        check(
            spec.get("restart") == "unless-stopped",
            f"service {svc!r} must set `restart: unless-stopped` -- there is no "
            "cross-project depends_on, so restart is the only ordering mechanism",
        )
        expected_svc_nets = EXPECTED_SERVICE_NETWORKS.get(svc)
        if expected_svc_nets is not None:
            check(
                service_networks(spec) == expected_svc_nets,
                f"service {svc!r} must join exactly "
                f"{sorted(expected_svc_nets)}, got "
                f"{sorted(service_networks(spec))} -- a top-level network no "
                "service joins is silently ignored, and an extra one added here "
                "would put this service on the backend bridge it deliberately "
                "stays off; neither `docker compose config` nor `up` objects to "
                "either mistake",
            )
        for dep in spec.get("depends_on") or {}:
            check(
                dep in services,
                f"service {svc!r} declares depends_on {dep!r}, which is not in "
                "this compose project -- depends_on cannot cross projects",
            )

    for svc in NO_PORTS_SERVICES:
        check(svc in services, f"expected a {svc!r} service in this compose project")
        check(
            not (services.get(svc) or {}).get("ports"),
            f"service {svc!r} must publish no ports -- it is reachable only "
            "from other containers on poc-net, never from the host",
        )

    # The deploy builds paperclip's image straight into the host's image store
    # and never pushes it anywhere, so there is nothing to pull. `never` is
    # what makes a missing image fail CLOSED: without it Compose's default
    # policy would reach for GHCR, which still carries the tags the retired
    # publish-then-pull scheme pushed -- so a rollback dispatch naming a tag
    # that is no longer in the host's store would silently start a stale
    # registry image instead of failing.
    check(
        (services.get("paperclip") or {}).get("pull_policy") == "never",
        "service 'paperclip' must set `pull_policy: never` -- its image is "
        "built directly into the deploy host's image store and never pushed, "
        f"got {(services.get('paperclip') or {}).get('pull_policy')!r}. "
        "Without it a missing image silently falls back to GHCR, where the "
        "retired publish-then-pull scheme left tags a rollback could start by "
        "accident",
    )
    # The converse, and it is not symmetry for its own sake: postgres:17-alpine
    # is an upstream image this workflow never builds, so `never` on it would
    # break every fresh host with "image not found" the first time the stack
    # comes up.
    check(
        (services.get("postgres") or {}).get("pull_policy") is None,
        "service 'postgres' must NOT set a `pull_policy` -- postgres:17-alpine "
        "is an upstream image nothing here builds, so it has to stay fetchable "
        "for the first `up -d` on a host that does not have it yet",
    )

    # depends_on postgres is preserved on purpose: unlike the cross-project
    # links this split had to drop, postgres is in THIS project.
    paperclip_deps = (services.get("paperclip") or {}).get("depends_on") or {}
    check(
        (paperclip_deps.get("postgres") or {}).get("condition") == "service_healthy",
        "paperclip must keep `depends_on: postgres: condition: service_healthy` "
        "-- postgres is in the same compose project, so this ordering survives "
        "the split and should not be dropped",
    )

    check_env_example_declares_every_reference()
    check_compose_renders()
    check_deploy_workflow()
    return report()


if __name__ == "__main__":
    sys.exit(main())
