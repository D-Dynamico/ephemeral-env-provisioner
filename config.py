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
    max_environments_per_user: int = 5
    base_port: int = 20000


settings = Settings()