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
    """Every DOCKER_HOST value in the workflow, with where it was found.

    Keyed on the exact name `DOCKER_HOST`, so sibling variables that merely
    start with `DOCKER_` -- notably the job-level `DOCKER_CONFIG` that isolates
    this job's registry credential and buildx state -- are not collected here
    and cannot trip the DOCKER_HOST assertions.
    """
    found: list[tuple[str, str]] = []
    for key, value in (doc.get("env") or {}).items():
        if key == "DOCKER_HOST":
            found.append(("workflow-level env", str(value)))
    for job_name, job in (doc.get("jobs") or {}).items():
        for key, value in (((job or {}).get("env")) or {}).items():
            if key == "DOCKER_HOST":
                found.append((f"job {job_name!r} env", str(value)))
        for step in (job or {}).get("steps") or []:
            for key, value in (((step or {}).get("env")) or {}).items():
                if key == "DOCKER_HOST":
                    found.append((f"step {(step or {}).get('name')!r} env", str(value)))
    return found


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
    deploy runs against the staging host's daemon only via step-scoped
    DOCKER_HOST (never a persistent `docker context`, and never at workflow or
    job level, which would leak into the build step), that the external
    volume and the paperclip-home bind-mount source are verified rather than
    unconditionally pre-created, that compose is invoked with --env-file so
    secrets never hit a shell command string, that a pull always precedes
    `up -d`, and the ways this workflow could otherwise leak or weaken
    credentials.
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
    check(
        any("--env-file" in ln for ln in script_lines),
        "compose must be invoked with --env-file so secret values are never "
        "interpolated into a shell command string",
    )
    # (line index, character offset within the line) rather than just a line
    # index: two tuples compare lexicographically, so this also gets a
    # same-line `docker compose pull && docker compose up -d` right -- with
    # line index alone, both halves share one index and `min(...) < min(...)`
    # would compare `i < i` and always be False, even though `pull` runs
    # first.
    pull_positions = [
        (i, ln.find(" pull"))
        for i, ln in enumerate(script_lines)
        if "compose" in ln and " pull" in ln
    ]
    up_positions = [
        (i, ln.find("up -d"))
        for i, ln in enumerate(script_lines)
        if "compose" in ln and "up -d" in ln
    ]
    check(
        bool(pull_positions) and bool(up_positions) and min(pull_positions) < min(up_positions),
        "`compose pull` must precede `compose up -d`, or a deploy can silently "
        "run a stale image already present on the host",
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

    # DOCKER_HOST must be scoped to individual steps, never the workflow or
    # the job. Either would also apply to the build step, and buildx binds to
    # whichever daemon is current -- so the image would build on the deploy
    # host instead of the runner.
    job_level_docker_host = [
        name for name, job in (doc.get("jobs") or {}).items()
        if "DOCKER_HOST" in set(((job or {}).get("env")) or {})
    ]
    non_step_docker_host = job_level_docker_host + (
        ["workflow"] if "DOCKER_HOST" in set(doc.get("env") or {}) else []
    )
    check(
        not non_step_docker_host,
        f"DOCKER_HOST must never be set at workflow or job level (found on "
        f"{non_step_docker_host}) -- either would apply to the build step "
        "too, and buildx binds to whichever daemon is current, so the image "
        "would build on the deploy host instead of the runner",
    )
    # "At least one step has it" is not enough: dropping DOCKER_HOST from
    # any ONE of these three specifically would silently redirect that
    # step's docker/compose calls to the runner's own daemon instead of the
    # deploy host's, while every other DOCKER_HOST-bearing step stays green.
    # Each is asserted by name.
    for step_name in ("Verify host preconditions", "Pull and deploy", "Smoke test"):
        named_step = _step_by_name(doc, step_name)
        check(
            named_step is not None,
            f"no step named {step_name!r} -- expected one of the steps that "
            "must run against the deploy host's daemon",
        )
        if named_step is not None:
            check(
                "DOCKER_HOST" in _step_env_keys(named_step),
                f"the {step_name!r} step must have DOCKER_HOST in its own "
                "`env:` -- without it this step's docker/compose calls would "
                "silently run against the runner's own daemon instead of the "
                "deploy host's",
            )

    # The build step must run against the runner's OWN daemon, never the
    # deploy host's -- it must not inherit DOCKER_HOST from anywhere.
    build_step = _step_with_uses_containing(doc, "docker/build-push-action")
    check(
        build_step is not None,
        "no step uses docker/build-push-action -- build and deploy are "
        "unified in this workflow now that trc-publish.yml is deleted, so "
        "the image must be built here",
    )
    if build_step is not None:
        check(
            "DOCKER_HOST" not in _step_env_keys(build_step),
            "the docker/build-push-action step must not have DOCKER_HOST in "
            "its own `env:` -- buildx binds to whichever daemon DOCKER_HOST "
            "points at, so this would build the image on the deploy host "
            "instead of the runner",
        )
        check(
            (build_step.get("with") or {}).get("target") == "production",
            "the docker/build-push-action step must pin `target: production` "
            "-- the Dockerfile declares a later `cloud` stage, and omitting "
            "`target:` would silently publish that stage instead",
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

    # Matched against comment-stripped lines, not the raw step text: the raw
    # text has no comment-stripping at all, so a comment merely NAMING
    # id_rsa/.env.staging/docker logout (after the real line was deleted)
    # would satisfy this "must appear" check and leave the validator green
    # while the secret stays on the runner -- the false-pass shape a
    # reviewer flagged as the dangerous one.
    cleanup_step = None
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            if str((step or {}).get("if", "")).strip() != "always()":
                continue
            step_text = "\n".join(_script_lines((step or {}).get("run") or ""))
            if "id_rsa" in step_text and ".env.staging" in step_text and "docker logout" in step_text:
                cleanup_step = step
                break
        if cleanup_step is not None:
            break
    check(
        cleanup_step is not None,
        "an `if: always()` cleanup step must exist that removes id_rsa and "
        ".env.staging and logs out of the registry (`docker logout`) -- the "
        "runner is persistent, so every secret this workflow writes to disk "
        "must be removed even when an earlier step fails",
    )
    if cleanup_step is not None:
        # Killing the agent was ungated. Deleting $RUNNER_TEMP/id_rsa does not
        # unload the key: a leaked ssh-agent keeps the DECRYPTED private key in
        # memory on a persistent runner, reachable by anything that can guess
        # or read the socket path, for as long as that agent lives -- and a new
        # one is started on every dispatch.
        check(
            "ssh-agent -k" in "\n".join(_script_lines(cleanup_step.get("run") or "")),
            "the `if: always()` cleanup step must run `ssh-agent -k` -- "
            "removing the key FILE does not unload the key, and a leaked agent "
            "holds the decrypted private key in memory on this persistent "
            "runner until the machine reboots, with one more leaked per "
            "dispatch",
        )

    # Repo-specific: the paperclip-home guards must run on plain SSH, never
    # under DOCKER_HOST -- a DOCKER_HOST proxies the Docker API, not a shell,
    # so it cannot stat a path on the host filesystem.
    guard_step = _step_by_name(doc, "Verify the paperclip-home guards")
    check(
        guard_step is not None,
        "no step named 'Verify the paperclip-home guards' -- the deploy must "
        "verify /srv/trc/staging/paperclip-home before rendering any secret",
    )
    if guard_step is not None:
        check(
            "DOCKER_HOST" not in _step_env_keys(guard_step),
            "the paperclip-home guard step must not have DOCKER_HOST in its "
            "own `env:` -- these checks stat a path on the host filesystem, "
            "which a DOCKER_HOST (an API proxy, not a shell) cannot do",
        )
        guard_script = "\n".join(_script_lines(guard_step.get("run") or ""))
        check(
            "stat -Lc" in guard_script,
            "the paperclip-home guard must read ownership with `stat -Lc` "
            "(the -L dereferences symlinks, since both `[ -d ]` and Docker's "
            "bind mount follow them)",
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
