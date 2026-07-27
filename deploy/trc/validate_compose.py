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
    if key.endswith("_DIR"):
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


def _run_script_lines(doc: dict) -> list[str]:
    """Every executable line of every `run:` block, full-line shell comments dropped.

    Assertions about script behaviour cannot match the raw file text: these
    scripts document the rules they follow, so a comment saying "never set -x"
    reads as a violation and a comment saying "serialize on flock" reads as
    compliance. Only executable lines carry either meaning.
    """
    lines: list[str] = []
    for job in (doc.get("jobs") or {}).values():
        for step in ((job or {}).get("steps") or []):
            script = (step or {}).get("run")
            if not script:
                continue
            lines += [
                ln for ln in script.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")
            ]
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


def check_deploy_workflow() -> None:
    """Assert the deploy workflow's security and reproducibility invariants.

    These are properties a generic YAML linter cannot know about: the digest
    pin that makes rollback a re-dispatch, the host-side mutex that stands in
    for a cross-repository concurrency group, the two ways this workflow could
    leak or weaken credentials, and the one specific to this repo -- the
    deploy must never create the /paperclip bind mount, because an empty
    directory would let Paperclip bootstrap a fresh unclaimed instance over
    data that should have been migrated.
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
        "image_digest" in inputs,
        "workflow_dispatch must take an `image_digest` input",
    )
    check(
        bool((inputs.get("image_digest") or {}).get("required")),
        "`image_digest` must be required -- deploys are digest-pinned so that "
        "rollback is a re-dispatch with the previous digest",
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
        any("flock" in ln for ln in script_lines),
        "no `run:` block calls flock: three repositories deploy into one host "
        "and a GitHub concurrency group cannot span repositories",
    )
    check(
        "StrictHostKeyChecking=no" not in raw
        and "StrictHostKeyChecking no" not in raw,
        "host keys must be pinned via TRC_SSH_KNOWN_HOSTS, not bypassed",
    )

    # Both the literal path and the variable spellings: `mkdir -p
    # "$PAPERCLIP_HOME_DIR"` creates exactly the same directory as `mkdir -p
    # /srv/trc/staging/paperclip-home`, so matching only the literal would let
    # the ban be reintroduced by name.
    created = [
        ln.strip()
        for ln in script_lines
        if re.search(
            r"\bmkdir\b[^\n]*(paperclip-home|\$\{?PAPERCLIP_HOME_DIR\}?)", ln
        )
    ]
    check(
        not created,
        f"the deploy must NOT create the /paperclip bind mount, but {created} "
        "does. That directory holds secrets/master.key and the only copy of the "
        "hermes_gateway wiring, so a typo'd path must fail loudly instead of "
        "being created empty -- which is also why `allow_bootstrap` relaxes only "
        "the instance-state check and never this one. Creating it here would let "
        "a mistyped path be silently accepted as a fresh install",
    )

    volume_creates = [
        ln.strip()
        for ln in script_lines
        if re.search(r"\bdocker\s+volume\s+create\b", ln)
    ]
    check(
        not volume_creates,
        f"{volume_creates} pre-creates a volume on the host. Compose REFUSES to "
        "start when an external volume is missing, and that fail-closed "
        "behaviour is the entire point of declaring the volume external: "
        "creating it here converts a loud failure into a silently EMPTY volume "
        "and the run goes green -- Postgres initialises a brand-new database, "
        "Paperclip comes up with no data, and every smoke test still passes. "
        "The volume is created during the Phase 2 migration, never by a deploy. "
        "`docker network create poc-net` is fine and stays: a network carries "
        "no data",
    )

    unguarded_flock = [
        ln.strip()
        for ln in script_lines
        if "flock" in ln and "-c" in ln and not re.search(r"-c\s*'set -e\b", ln)
    ]
    check(
        not unguarded_flock,
        f"{unguarded_flock} runs a `flock -c` script whose first statement is "
        "not `set -e`. The -c string is a SEPARATE shell, so the enclosing "
        "`set -eu` does not reach into it: without it a failed `docker compose "
        "pull` is ignored and `up -d` silently redeploys the image already on "
        "the host while the run reports success. The style this asserts is "
        "`flock <lock> -c 'set -e` with the commands on the following lines",
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
