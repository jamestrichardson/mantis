# syntax=docker/dockerfile:1
#
# Two-stage build: install Mantis and its runtime dependencies into an
# isolated prefix in the builder stage, then copy only that prefix into a
# clean slim image. No dev/test dependencies, no build toolchain, no
# source-tree cruft ends up in the final image.

FROM python:3.14-slim AS builder

WORKDIR /build

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.14-slim AS runtime

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin mantis

COPY --from=builder /install /usr/local

# API port (mantis serve, #21/#83) and metrics port (mantis.observability.metrics,
# #66). `mantis serve` is the production entry point (see CMD below) and,
# unlike a one-shot CLI invocation, is exactly the persistent process
# that's supposed to hold both ports open for its lifetime -- see
# docs/observability.md and docs/api.md. `MANTIS_METRICS_ENABLED=false`
# still opts back out if an operator doesn't want the metrics port
# exposed at all; `mantis <agent>`/`mantis eval` one-shot invocations
# (e.g. via `docker compose exec`) keep defaulting the metrics server
# off, unaffected by this image-level EXPOSE.
EXPOSE 8080
EXPOSE 9108

USER mantis
WORKDIR /home/mantis

ENTRYPOINT ["mantis"]
CMD ["serve"]
