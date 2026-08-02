"""
`GET /metrics` — Prometheus exposition for the API process.

Behind `X-API-Key`, like every route that is not `/health` or the docs. The
endpoint is low-value to an attacker today, but §9's posture is that this
service fails closed and does not grow an unauthenticated surface by default;
a scraper can send a header.

**This serves the API's registry only.** Provision and teardown outcomes are
counted in the worker, in a different container, and are scraped separately
from `settings.worker_metrics_port`. That means this endpoint currently exposes
no application counters at all — the API increments none of the three metrics.
Serving the worker's numbers here would mean reading its mmapped files across a
container boundary, which works only while both happen to sit on one host, and
would quietly make the single-node assumption permanent.
"""

from fastapi import APIRouter, Depends, Response

from api.auth import require_principal
from metrics import render_latest

router = APIRouter(
    tags=["meta"],
    dependencies=[Depends(require_principal)],
    responses={401: {"description": "Missing or invalid X-API-Key header"}},
)


@router.get(
    "/metrics",
    summary="Prometheus metrics for the API process",
    response_class=Response,
)
async def metrics() -> Response:
    payload, content_type = render_latest()
    return Response(content=payload, media_type=content_type)
