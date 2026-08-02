"""
Metric definitions.

The label sets get their own tests because the constraint they encode is
invisible at the call site. `provision_total.labels(...)` looks equally correct
whichever labels it names, and a metric labelled by `env_id` or `owner` costs
one time series per environment forever and leaks a principal's identity into an
endpoint meant to be scraped by something else (CLAUDE.md §9). Nothing else in
the test suite would notice.
"""

from prometheus_client import CONTENT_TYPE_LATEST

from config import settings
from metrics import (
    ALL_METRICS,
    FORBIDDEN_LABELS,
    OUTCOMES,
    Outcome,
    provision_duration_seconds,
    provision_total,
    render_latest,
    teardown_total,
)


# ── Label sets ────────────────────────────────────────────────────────────────

def test_label_names_are_exactly_as_specified():
    assert provision_total._labelnames == ("template", "outcome")
    assert teardown_total._labelnames == ("outcome",)
    assert provision_duration_seconds._labelnames == ("template",)


def test_no_metric_carries_an_unbounded_label():
    """
    The rule this suite exists for. A new metric labelled by env_id or owner
    fails here rather than in production six months later.
    """
    for metric in ALL_METRICS:
        offending = FORBIDDEN_LABELS.intersection(metric._labelnames)
        assert not offending, f"{metric._name} is labelled by {sorted(offending)}"


def test_outcome_is_a_closed_set_of_three():
    assert OUTCOMES == {"success", "failure", "retry"}
    # retry must stay distinct from failure: both tasks retry, and folding them
    # together reports a recovered transient error as a lost environment.
    assert Outcome.RETRY != Outcome.FAILURE


# ── Exposed names ─────────────────────────────────────────────────────────────

def test_counter_names_expose_without_a_doubled_suffix():
    """
    prometheus_client strips a `_total` suffix from a Counter's name and adds it
    back when rendering. Constructing `Counter("provision")` would therefore
    also expose `provision_total`, and constructing `Counter("provision_total")`
    does not expose `provision_total_total`. Pinned because the naming in
    CLAUDE.md §11.2 is exact.
    """
    body = render_latest()[0].decode()
    assert "# TYPE provision_total counter" in body
    assert "# TYPE teardown_total counter" in body
    assert "provision_total_total" not in body


def test_render_latest_uses_the_prometheus_content_type():
    _, content_type = render_latest()
    assert content_type == CONTENT_TYPE_LATEST
    assert content_type.startswith("text/plain")


# ── Buckets ───────────────────────────────────────────────────────────────────

def test_duration_buckets_span_the_container_ready_timeout():
    """
    A provision cannot take longer than the readiness timeout without failing,
    so buckets must reach it — and past it, or the slowest real successes are
    indistinguishable from each other in the overflow bucket.
    """
    bounds = provision_duration_seconds._upper_bounds
    finite = [b for b in bounds if b != float("inf")]

    assert max(finite) > settings.container_ready_timeout_seconds
    # The default buckets stop at 10s, which is roughly one observed provision.
    assert max(finite) > 10
    assert bounds[-1] == float("inf")
