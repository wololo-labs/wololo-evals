"""Render a per-engine Inspect **docker sandbox** spec from a container trial
wiring — the missing PROVISIONING half of the live docker-sandbox path.

``sandbox().exec(env=...)`` can only pass env into an already-provisioned
container; it can NOT choose the runner image, attach credential mounts, or add
``host.docker.internal``. Those are provisioning-time concerns and belong in the
compose file Inspect brings the sandbox up from. This module renders exactly
that compose file for one engine's runner image:

- ``image:``       the pinned per-engine runner image (``wololo-evals/…-runner``),
- ``volumes:``     the read-only credential bind-mounts (host cred file -> the
                   path the CLI reads), so auth arrives via the mounted file,
- ``extra_hosts:`` ``host.docker.internal:host-gateway`` so the container reaches
                   the host fixture server,
- ``environment:`` the NON-secret ``WOLOLO_FIXTURE_URL`` only.

Security contract: **no secret value is ever written to the compose file on
disk.** The auth env KEY *names* are known (``plan.env_keys``) but their VALUES
are resolved lazily at exec time by ``container_auth.resolve_exec_env`` and
injected through ``sandbox().exec(env=...)`` (the wiring's ``exec_factory``),
never baked into the compose YAML. Credential material reaches the container
only through the read-only bind-mounts and that exec-time env.

Boundary: this module imports **no** ``inspect_ai`` (that stays confined to
``inspect_adapter.py``). It returns a compose-file path; the adapter turns the
``("docker", <path>)`` shorthand into the Task's sandbox spec. Stdlib + PyYAML
only (PyYAML is already an ``inspect_ai`` dependency).
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from typing import TYPE_CHECKING, Iterator, Sequence

import yaml

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import of the adapter
    from ..inspect_adapter import ContainerTrialWiring

__all__ = [
    "DEFAULT_KEEP_ALIVE",
    "SERVICE_NAME",
    "render_compose_yaml",
    "sandbox_compose_file",
]

#: Inspect resolves the default sandbox by a service literally named ``default``
#: (or one carrying ``x-default: true``); we use the plain name.
SERVICE_NAME = "default"

#: The runner images' ENTRYPOINT is ``exec "$@"``; giving it a keep-alive command
#: makes the container idle so Inspect can ``docker compose exec`` each trial step
#: into it (the harness loop, git, the acceptance test) rather than the container
#: exiting immediately. Mirrors the image CMD, made explicit so provisioning does
#: not depend on the image's default CMD.
DEFAULT_KEEP_ALIVE: tuple[str, ...] = ("sleep", "infinity")


def render_compose_yaml(
    wiring: "ContainerTrialWiring",
    *,
    service_name: str = SERVICE_NAME,
    keep_alive: Sequence[str] = DEFAULT_KEEP_ALIVE,
) -> str:
    """Render the docker-compose YAML that provisions ``wiring``'s runner-image
    sandbox. Secret-free: only the image name, read-only credential mounts, the
    ``host.docker.internal`` extra-host, and the non-secret ``WOLOLO_FIXTURE_URL``
    ever appear. Auth env VALUES are deliberately absent — they are injected at
    exec time via ``sandbox().exec(env=...)`` and must never touch disk.

    Raises ``ValueError`` when the harness has no runner image (nothing to
    provision) so a mis-wired live run fails loudly rather than falling back to
    Inspect's generic default image.
    """
    if wiring.image is None:
        raise ValueError(
            f"harness {wiring.harness!r} has no runner image; cannot render a "
            "per-engine docker sandbox spec (only harnesses with a "
            "wololo-evals/<engine>-runner image can be provisioned)"
        )

    service: dict[str, object] = {
        "image": wiring.image,
        # keep-alive so the container idles for docker-exec trial steps.
        "command": list(keep_alive),
        "init": True,
        # host.docker.internal:host-gateway — the container reaches the host
        # fixture server (the loopback fixture URL is rewritten onto this host).
        "extra_hosts": [wiring.add_host],
        "stop_grace_period": "1s",
    }

    # NON-secret env only. WOLOLO_FIXTURE_URL is a host.docker.internal URL, safe
    # on disk. Auth KEYs are intentionally NOT emitted here.
    if wiring.fixture_url:
        service["environment"] = {"WOLOLO_FIXTURE_URL": wiring.fixture_url}

    # Read-only credential bind-mounts (short syntax "host:container:ro"). Each
    # mount's host file is guaranteed to exist (container_auth only adds a mount
    # for a present file); read_only is always True so a trial cannot tamper with
    # host auth. The VALUE inside the file is never parsed here.
    volumes: list[str] = []
    for mount in wiring.mounts:
        mode = "ro" if mount.read_only else "rw"
        volumes.append(f"{mount.host_path}:{mount.container_path}:{mode}")
    if volumes:
        service["volumes"] = volumes

    doc = {"services": {service_name: service}}
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


@contextlib.contextmanager
def sandbox_compose_file(
    wiring: "ContainerTrialWiring",
    *,
    service_name: str = SERVICE_NAME,
    keep_alive: Sequence[str] = DEFAULT_KEEP_ALIVE,
    dir: str | None = None,
) -> Iterator[str]:
    """Materialise the rendered compose YAML into a throwaway file and yield its
    absolute path, cleaning it up on exit. Inspect reads the compose file from
    the host, so its location is unconstrained (the mount *sources* inside it are
    absolute host paths); the file is removed when the ``with`` block ends.

    The yielded path is what the adapter wraps as ``("docker", <path>)`` for the
    Task's ``sandbox=`` argument.
    """
    yaml_text = render_compose_yaml(
        wiring, service_name=service_name, keep_alive=keep_alive
    )
    tmpdir = tempfile.mkdtemp(prefix="wololo-sandbox-", dir=dir)
    try:
        path = os.path.join(tmpdir, "compose.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(yaml_text)
        yield path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
