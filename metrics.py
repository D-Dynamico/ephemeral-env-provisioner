"""
Prometheus metrics, shared by the API and the worker.

Definitions live here for the same reason logging configuration lives in
`observability.py`: more than one process needs them and a second definition
would be a second answer.

Two things about this are not obvious.

**The increments and the HTTP surface are in different processes.** Provision
and teardown outcomes are only knowable in the Celery worker; the service's HTTP
surface is the API container. They do not share a registry, a process or a
filesystem, so there is no arrangement in which one endpoint serves both. There
are two scrape targets: `/metrics` on the API, and a plain HTTP server on
`settings.worker_metrics_port` in the worker.

**The worker forks.** It runs a prefork pool, so the process that answers the
scrape is not the process that counted anything. `prometheus_client` handles
this only in multiprocess mode, driven by the `PROMETHEUS_MULTIPROC_DIR`
environment variable: each child writes to mmapped files in that directory and
the parent's collector sums them. Without it, every increment made in a child is
lost when the child exits — with a `--concurrency=2` pool, that is most of them.
The variable is read by the library from the real environment, so it cannot move
into `config.py`; it is set on the worker service in `docker-compose.yml`.

Labels are the other constraint, and it is a hard one. **Never label a metric by
`env_id` or `owner`** (CLAUDE.md §9). Each is unbounded — one new time series
per environment, retained for as long as the scraper keeps history — and `owner`
additionally puts a principal's identity into an endpoint that exists to be
scraped by something other than this service. `template` and `outcome` are
closed sets, which is why they are the only labels here.
"""

import os
import shutil

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
    start_http_server,
)

# Read by prometheus_client itself, not by us. Present means multiprocess mode.
MULTIPROC_ENV_VAR = "PROMETHEUS_MULTIPROC_DIR"


class Outcome:
    """
    The full `outcome` label set. Three values, not two.

    `RETRY` is separate from `FAILURE` on purpose: both tasks retry, so an
    attempt that failed and will be tried again is a different event from one
    that gave up. Folding them together makes `*_total{outcome="failure"}`
    read as a burst of lost environments whenever a transient Docker error
    resolves itself on the second attempt.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    RETRY = "retry"


OUTCOMES = frozenset({Outcome.SUCCESS, Outcome.FAILURE, Outcome.RETRY})

# Label value for a provision that fails before its row could be read, so the
# template is genuinely not known. Keeps the label set closed — the templates
# allowlist plus this one sentinel — rather than leaving the series unlabelled.
UNKNOWN_TEMPLATE = "unknown"

# The default buckets stop at 10s and an observed provision takes about that
# long, so every real one would land in the overflow bucket and the histogram
# would carry no information. These span container_ready_timeout_seconds (120)
# with room above it.
PROVISION_BUCKETS = (1, 2.5, 5, 10, 20, 30, 60, 120, 300, float("inf"))

provision_total = Counter(
    "provision_total",
    "Provision attempts by template and outcome.",
    ["template", "outcome"],
)

teardown_total = Counter(
    "teardown_total",
    "Teardown attempts by outcome.",
    ["outcome"],
)

provision_duration_seconds = Histogram(
    "provision_duration_seconds",
    "Seconds from the provision task starting to the environment reaching "
    "RUNNING. Successful provisions only.",
    ["template"],
    buckets=PROVISION_BUCKETS,
)

# Asserted in tests. A metric added later with an unbounded label is the failure
# this list exists to catch, and it is not visible at the call site.
FORBIDDEN_LABELS = frozenset({"env_id", "owner", "name", "id"})

ALL_METRICS = (provision_total, teardown_total, provision_duration_seconds)


# ── API side ──────────────────────────────────────────────────────────────────

def render_latest() -> tuple[bytes, str]:
    """
    Render the calling process's own registry.

    The API is a single uvicorn process and counts nothing itself, so it needs
    no multiprocess collector — and deliberately does not read the worker's
    directory. Reaching into another container's mmapped files would work only
    because both happen to be on one host today, which is exactly the coupling
    that makes a single-node assumption permanent.
    """
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


# ── Worker side ───────────────────────────────────────────────────────────────

def prepare_multiproc_dir() -> str | None:
    """
    Create the multiprocess directory empty. Returns None when not in
    multiprocess mode, which is the normal case under pytest.

    Wiping matters: the files are not cleaned up when the worker stops, so a
    restart would otherwise collect the previous run's counters and serve them
    as current. A counter that resurrects is worse than one that resets, since
    `rate()` reads the jump as real traffic.

    Called from `worker_init`, which fires in the parent before the pool forks.
    Every metric here is labelled and so writes no file until something
    increments it, meaning there is nothing live to wipe out from under.
    """
    path = os.environ.get(MULTIPROC_ENV_VAR)
    if not path:
        return None

    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)
    return path


def start_worker_metrics_server(port: int) -> None:
    """
    Serve the worker's metrics on `port`.

    In multiprocess mode this collects from the shared directory, so it reports
    the pool's totals rather than the parent's (which are zero — the parent
    executes no tasks).

    This server has no authentication. `prometheus_client`'s HTTP server has no
    hook for it, unlike the API's `/metrics`, which sits behind `X-API-Key`.
    The port is therefore never published to the host in `docker-compose.yml`;
    it is reachable only from inside the compose network. That is a real
    asymmetry with §9 rather than a solved problem, and it is written down in
    the README as such.
    """
    if os.environ.get(MULTIPROC_ENV_VAR):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    else:
        registry = REGISTRY

    start_http_server(port, registry=registry)


def mark_worker_process_dead(pid: int | None) -> None:
    """
    Release a finished child's per-process files.

    This clears gauge files only. Counter and histogram files are left behind
    deliberately: their samples must survive the child that wrote them or a
    pool recycling its workers would lose every count it had made. There are no
    gauges here today, which is also why multiprocess mode needs no aggregation
    choice — those are declared per gauge, and getting one wrong is the usual
    way this feature breaks.
    """
    if pid is None or not os.environ.get(MULTIPROC_ENV_VAR):
        return
    multiprocess.mark_process_dead(pid)
