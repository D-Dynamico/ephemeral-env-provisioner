"""
Logging configuration, shared by the API, the worker and beat.

Three processes write logs. Before this module the API configured structlog as
a side effect of importing `api.main`, and the worker did not configure it at
all — so the same event rendered three different ways depending on which
process emitted it, and nothing downstream could parse the result.

Two things matter here:

* **`env_id` is a field, not a prefix.** `bind_env_id` puts it in a contextvar,
  so every line emitted while a task runs carries it — including lines from
  `docker_manager`, which never receives the id as an argument. Correlating a
  provision across the worker and the Docker layer is then a filter, not a grep.
* **JSON outside development.** Logs are assumed to be shipped somewhere less
  trusted than the host (§9), so they are machine-readable by default and
  human-readable only when `APP_ENV=development`.

Nothing here writes a log line itself. What must never be logged is unchanged
and is a caller's responsibility: resolved template `environment` values carry
credentials (§9).
"""

import logging
from contextlib import contextmanager

import structlog

from config import settings

# configure_logging is called from three import paths in the same process
# (api.main, worker.tasks, docker_manager.compose). Reconfiguring is not
# harmful, but it would stack a second handler on the root logger and duplicate
# every stdlib record.
_configured = False


def configure_logging() -> None:
    """Idempotent. Safe to call from every module that logs."""
    global _configured
    if _configured:
        return

    json_output = settings.app_env != "development"

    # Applied to structlog events and, via foreign_pre_chain, to stdlib records
    # from Celery, uvicorn and the Docker SDK — so third-party lines land in the
    # same shape as ours rather than beside it.
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    # ConsoleRenderer formats exceptions itself and warns if handed an already
    # formatted one, so the traceback processor belongs only on the JSON path.
    if json_output:
        render_chain: list = [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
    else:
        render_chain = [structlog.dev.ConsoleRenderer(colors=False)]

    structlog.configure(
        processors=shared_processors + [
            # Hands off to the stdlib handler below rather than rendering here,
            # so structlog and stdlib records go through one formatter.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *render_chain,
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Celery and uvicorn both install their own handlers. Leaving them attached
    # emits every record twice, once formatted and once not.
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    _configured = True


def get_logger(name: str | None = None):
    """Configure on first use, so no caller has to remember to."""
    configure_logging()
    return structlog.get_logger(name)


@contextmanager
def bind_env_id(env_id):
    """
    Tag every log line emitted inside the block with `env_id`.

    Bound as a contextvar rather than passed down the call stack: the point is
    that `DockerManager` lines are correlated too, and it takes `env_id` only
    as a name component, not as something it logs.

    Unbinds on the way out, including on exception. A Celery prefork child
    handles many environments in its lifetime, and a leaked contextvar would
    label the next environment's logs with the previous one's id — worse than
    no correlation, because it reads as fact.
    """
    token = structlog.contextvars.bind_contextvars(env_id=str(env_id))
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**token)
