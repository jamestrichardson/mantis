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

# Documents the metrics port this image is prepared to serve on, but
# does NOT start the metrics server by default: Mantis today runs as a
# short-lived CLI process per invocation (`docker compose exec mantis
# mantis ...`), not a resident service, so a default-on metrics server
# would bind 9108 (and contend for it under concurrent invocations) for
# a window that closes the moment the command exits. See
# docs/observability.md. Set MANTIS_METRICS_ENABLED=true explicitly for
# local/manual testing of the endpoint itself.
EXPOSE 9108

USER mantis
WORKDIR /home/mantis

ENTRYPOINT ["mantis"]
CMD ["--help"]
