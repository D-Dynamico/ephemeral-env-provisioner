import sys
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_docker_socket() -> str:
    """Return the correct Docker socket URL for the current OS."""
    if sys.platform == "win32":
        return "npipe:////./pipe/docker_engine"
    return "unix:///var/run/docker.sock"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_env: str = "development"
    secret_key: str = "change-me"

    # Auth. Comma-separated `key:principal` pairs, e.g.
    #   API_KEYS=k_local_dev_0123456789:dev@example.com,k_ci_9876543210abcd:ci
    # The principal owns whatever that key creates. Empty means unconfigured,
    # and the API refuses to start — an open Docker socket must fail closed.
    api_keys: str = ""

    # Comma-separated browser origins allowed to call the API. Empty means no
    # CORS headers are sent at all, which is the right default for a service
    # with no first-party web UI.
    cors_allow_origins: str = ""

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/provisioner"

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # Docker — override in .env if needed
    # Windows:  npipe:////./pipe/docker_engine
    # Linux/Mac: unix:///var/run/docker.sock
    docker_socket: str = _default_docker_socket()

    # Environments
    default_ttl_seconds: int = 7200
    # How long one container may take to become ready before the provision is
    # treated as failed. Must stay well under provisioning_timeout_seconds, or
    # the staleness sweep fails the row while the worker is still waiting.
    container_ready_timeout_seconds: int = 120
    max_environments_per_user: int = 5
    base_port: int = 20000

    # Per-container resource ceilings. These are a denial-of-service guard, not
    # a bin-packing budget: the quota bounds how many environments a principal
    # runs, not what each one consumes (§9). A template may tune them per role
    # but cannot opt out — `resource_limits` always returns all three.
    #
    # Set them near real usage and a normal provision becomes an OOM kill, which
    # fails the row. Headroom is deliberate.
    default_container_memory_limit: str = "512m"
    default_container_cpu_limit: float = 1.0    # cores; converted to nano_cpus
    default_container_pids_limit: int = 256     # fork-bomb ceiling

    # Reconciliation
    reap_interval_seconds: int = 60
    orphan_sweep_interval_seconds: int = 300
    # Docker resources younger than this are never swept, so a stack still
    # being provisioned is not removed out from under its worker.
    orphan_grace_seconds: int = 900

    # How long a row may sit in a transient state before it is presumed dead.
    # A worker that is killed (rather than raising) leaves no task behind to
    # move the row, so only a sweep can free it.
    stale_sweep_interval_seconds: int = 120
    pending_timeout_seconds: int = 600
    provisioning_timeout_seconds: int = 900
    stopping_timeout_seconds: int = 600

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]


settings = Settings()