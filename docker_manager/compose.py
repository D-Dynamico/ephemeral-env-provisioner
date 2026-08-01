"""
Docker manager — wraps the Docker SDK to provision and tear down
isolated stacks for each environment.

Phase 1: single-container webapp + postgres, port-mapped to host.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import docker
import docker.errors
from docker.models.containers import Container
from docker.models.networks import Network

from config import settings

log = logging.getLogger(__name__)

# Every resource this service creates carries this label. It is the only way to
# find resources again when the DB has no record of them (invariant 3), so the
# create path and the sweep must agree on it exactly — hence one constant.
LABEL_ENV_ID = "provisioner.env_id"
LABEL_ROLE = "provisioner.role"


@dataclass
class StackResult:
    """Everything the caller needs to know after a stack spins up."""
    network_id: str
    container_ids: list[str]
    host_port: int


@dataclass
class LabelledResources:
    """Docker resources found by label for a single env_id."""
    container_ids: list[str] = field(default_factory=list)
    network_ids: list[str] = field(default_factory=list)


def network_name(env_id: str) -> str:
    """Invariant 6: naming is derived, never free-form."""
    return f"env-{env_id}"


def container_name(env_id: str, role: str) -> str:
    """
    Invariant 6. Also the container's DNS name inside its own bridge network,
    which is how one role addresses another.
    """
    return f"env-{env_id}-{role}"


def resolve_environment(env_id: str, spec: dict, roles: list[str]) -> dict[str, str]:
    """
    Substitute per-environment values into a spec's `environment` block.

    A template is static but the addresses in it are not: the database lives at
    `env-{id}-db`, which is only known once the environment has an id. Each role
    in the template is available as `{<role>_host}`, plus `{env_id}`.

    Values are formatted with `str.format`, so a literal `{` in a template value
    would need doubling. No template needs one today; a `ValueError` here is a
    template bug, raised at provision time rather than producing a container
    with a half-substituted connection string.

    The result carries credentials. Never log it (§9).
    """
    context: dict[str, str] = {"env_id": env_id}
    for role in roles:
        context[f"{role}_host"] = container_name(env_id, role)

    resolved: dict[str, str] = {}
    for key, value in (spec.get("environment") or {}).items():
        if not isinstance(value, str):
            resolved[key] = value
            continue
        try:
            resolved[key] = value.format(**context)
        except KeyError as exc:
            raise ValueError(
                f"Template placeholder {exc} in {key!r} matches no role in this "
                f"template. Known: {sorted(context)}"
            ) from exc
    return resolved


def to_docker_healthcheck(spec_healthcheck: dict) -> dict:
    """
    Translate a template's healthcheck into the Docker API's shape.

    Docker wants nanoseconds; templates are written in seconds because a
    template is meant to be read. Docker runs the check *inside* the container,
    which is why this is used instead of probing from the worker: the worker is
    on a different network and never has a route to the environment.
    """
    hc: dict = {"test": spec_healthcheck["test"]}
    for key, field_name in (
        ("interval_seconds", "interval"),
        ("timeout_seconds", "timeout"),
        ("start_period_seconds", "start_period"),
    ):
        if key in spec_healthcheck:
            hc[field_name] = int(spec_healthcheck[key] * 1_000_000_000)
    if "retries" in spec_healthcheck:
        hc["retries"] = spec_healthcheck["retries"]
    return hc


def _created_at(obj) -> datetime:
    """
    Docker reports RFC3339 with nanosecond precision; Python only parses
    microseconds, so the fraction is truncated to six digits.
    """
    raw = obj.attrs.get("Created")
    if not raw:
        # No timestamp means it cannot be aged out safely; treat it as brand new.
        return datetime.now(timezone.utc)
    cleaned = re.sub(r"(\.\d{6})\d+", r"\1", raw.replace("Z", "+00:00"))
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ── Template definitions ───────────────────────────────────────────────────────
# Each template maps a name → list of container specs.
# Phase 1 keeps this in-process; Phase 2+ can load from YAML files.

TEMPLATES: dict[str, list[dict]] = {
    "webapp-postgres": [
        {
            "role": "db",
            "image": "postgres:16-alpine",
            "environment": {
                "POSTGRES_USER": "app",
                "POSTGRES_PASSWORD": "app",
                "POSTGRES_DB": "app",
            },
            "expose_port": None,       # internal only
            "internal_port": 5432,
            # Postgres accepts connections some seconds after the container
            # reports "running". Anything that connects on boot must wait for
            # this, not for the process to exist.
            "healthcheck": {
                "test": ["CMD-SHELL", "pg_isready -U app -d app"],
                "interval_seconds": 2,
                "timeout_seconds": 3,
                "retries": 15,
                "start_period_seconds": 2,
            },
        },
        {
            "role": "app",
            "image": "kennethreitz/httpbin",  # lightweight demo app
            "environment": {},
            "expose_port": 80,          # this port gets mapped to a random host port
            "internal_port": 80,
            "depends_on_role": "db",    # start db first
        },
    ],
}


class DockerManager:
    def __init__(self) -> None:
        self._client: docker.DockerClient | None = None

    @property
    def client(self) -> docker.DockerClient:
        """
        Connect lazily. The API process imports this module (via worker.tasks)
        but never touches Docker, and tests must run without a daemon — so
        connecting in __init__ would make both fail at import time.
        """
        if self._client is None:
            self._client = docker.DockerClient(base_url=settings.docker_socket)
        return self._client

    # ── Public API ─────────────────────────────────────────────────────────────

    def provision(self, env_id: str, template_name: str) -> StackResult:
        """
        Spin up a full isolated stack for the given environment ID.
        Returns StackResult with network_id, container_ids, and host_port.
        """
        template = TEMPLATES.get(template_name)
        if not template:
            raise ValueError(f"Unknown template: {template_name!r}")

        network = self._create_network(env_id)
        log.info("Created network %s for env %s", network.id, env_id)

        container_ids: list[str] = []
        host_port: int | None = None
        role_to_container: dict[str, Container] = {}

        # Sort specs so containers with depends_on_role come last
        specs = sorted(template, key=lambda s: 0 if "depends_on_role" not in s else 1)
        roles = [s["role"] for s in template]

        for spec in specs:
            container = self._start_container(
                env_id=env_id,
                spec=spec,
                network=network,
                environment=resolve_environment(env_id, spec, roles),
            )
            container_ids.append(container.id)
            role_to_container[spec["role"]] = container
            log.info("Started container %s (role=%s)", container.short_id, spec["role"])

            # Wait before starting the next role, not after starting them all.
            # The dependency order is only meaningful if the dependency is
            # actually accepting connections by the time its dependant boots.
            self._wait_for_ready(container, spec)

            if spec.get("expose_port"):
                # Retrieve the dynamically assigned host port
                container.reload()
                bindings = container.ports.get(f"{spec['expose_port']}/tcp")
                if bindings:
                    host_port = int(bindings[0]["HostPort"])

        if host_port is None:
            raise RuntimeError("No exposed port found after provisioning")

        return StackResult(
            network_id=network.id,
            container_ids=container_ids,
            host_port=host_port,
        )

    def teardown(self, env_id: str, container_ids: list[str], network_id: str | None) -> None:
        """Stop and remove all containers and the network for an environment."""
        self.remove_resources(
            env_id=env_id,
            container_ids=container_ids,
            network_ids=[network_id] if network_id else [],
        )

    def remove_resources(
        self,
        env_id: str,
        container_ids: list[str],
        network_ids: list[str],
    ) -> None:
        """
        Remove the given containers and networks. Idempotent: anything already
        gone is a no-op, not an error (invariant 2). Containers are removed
        before networks, since a network with an attached container cannot be
        removed.
        """
        for cid in container_ids:
            try:
                container = self.client.containers.get(cid)
                container.stop(timeout=10)
                container.remove(force=True)
                log.info("Removed container %s (env=%s)", cid[:12], env_id)
            except docker.errors.NotFound:
                log.warning("Container %s already gone", cid[:12])
            except Exception as exc:
                log.error("Error removing container %s: %s", cid[:12], exc)

        for nid in network_ids:
            try:
                network = self.client.networks.get(nid)
                network.remove()
                log.info("Removed network %s (env=%s)", nid[:12], env_id)
            except docker.errors.NotFound:
                pass
            except Exception as exc:
                log.error("Error removing network %s: %s", nid[:12], exc)

    def find_labelled(self, min_age_seconds: int = 0) -> dict[str, LabelledResources]:
        """
        Group every Docker resource carrying LABEL_ENV_ID by that id.

        This is the actual state half of reconciliation: it finds resources the
        DB has no record of, which is the only way to recover from a provision
        that died after creating the network but before persisting its id
        (invariant 4).

        `min_age_seconds` skips resources younger than the grace period, so a
        stack still being built is never swept out from under its worker.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=min_age_seconds)
        found: dict[str, LabelledResources] = {}

        for container in self.client.containers.list(
            all=True, filters={"label": LABEL_ENV_ID}
        ):
            env_id = (container.labels or {}).get(LABEL_ENV_ID)
            if not env_id or _created_at(container) > cutoff:
                continue
            found.setdefault(env_id, LabelledResources()).container_ids.append(container.id)

        for network in self.client.networks.list(filters={"label": LABEL_ENV_ID}):
            env_id = (network.attrs.get("Labels") or {}).get(LABEL_ENV_ID)
            if not env_id or _created_at(network) > cutoff:
                continue
            found.setdefault(env_id, LabelledResources()).network_ids.append(network.id)

        return found

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _create_network(self, env_id: str) -> Network:
        return self.client.networks.create(
            name=network_name(env_id),
            driver="bridge",
            labels={LABEL_ENV_ID: env_id},
        )

    def _start_container(
        self,
        env_id: str,
        spec: dict,
        network: Network,
        environment: dict[str, str],
    ) -> Container:
        # `environment` is resolved by the caller and carries credentials.
        # It must not reach a log line (§9).
        role = spec["role"]
        ports = {}
        if spec.get("expose_port"):
            # Map container port → random host port (Docker picks it)
            ports = {f"{spec['expose_port']}/tcp": None}

        kwargs: dict = {}
        if spec.get("healthcheck"):
            kwargs["healthcheck"] = to_docker_healthcheck(spec["healthcheck"])

        container: Container = self.client.containers.run(
            image=spec["image"],
            name=container_name(env_id, role),
            detach=True,
            network=network.name,
            environment=environment,
            ports=ports,
            labels={
                LABEL_ENV_ID: env_id,
                LABEL_ROLE: role,
            },
            **kwargs,
        )
        return container

    def _wait_for_ready(
        self, container: Container, spec: dict, timeout: int | None = None
    ) -> None:
        """
        Block until the container is ready to be depended on.

        With a healthcheck in the spec, "ready" means Docker's own check passes
        — the process is accepting connections, not merely running. Without one,
        the best available signal is still `running`, which says nothing about
        whether the service inside has finished starting.

        Raises rather than returning a status: a stack whose database never came
        up is a failed provision, and the caller marks the row FAILED.
        """
        timeout = timeout or settings.container_ready_timeout_seconds
        wants_health = bool(spec.get("healthcheck"))
        deadline = time.time() + timeout

        while time.time() < deadline:
            container.reload()
            if container.status in ("exited", "dead"):
                raise RuntimeError(
                    f"Container {container.short_id} ({spec['role']}) died during startup"
                )
            if container.status == "running":
                if not wants_health:
                    return
                health = (
                    container.attrs.get("State", {}).get("Health", {}).get("Status")
                )
                if health == "healthy":
                    return
                if health == "unhealthy":
                    raise RuntimeError(
                        f"Container {container.short_id} ({spec['role']}) "
                        "failed its healthcheck"
                    )
            time.sleep(2)

        raise TimeoutError(
            f"Container {container.short_id} ({spec['role']}) was not ready "
            f"in {timeout}s"
        )


# Module-level singleton — import this everywhere
docker_manager = DockerManager()