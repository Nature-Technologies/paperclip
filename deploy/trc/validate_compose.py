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

    for svc, spec in services.items():
        check(
            spec.get("restart") == "unless-stopped",
            f"service {svc!r} must set `restart: unless-stopped` -- there is no "
            "cross-project depends_on, so restart is the only ordering mechanism",
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
    return report()


if __name__ == "__main__":
    sys.exit(main())
