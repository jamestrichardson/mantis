# syntax=docker/dockerfile:1
#
# Two-stage build: install Mantis and its runtime dependencies into an
# isolated prefix in the builder stage, then copy only that prefix into a
# clean slim image. No dev/test dependencies, no build toolchain, no
# source-tree cruft ends up in the final image.

FROM python:3.11-slim AS builder

WORKDIR /build

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim AS runtime

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin mantis

COPY --from=builder /install /usr/local

USER mantis
WORKDIR /home/mantis

ENTRYPOINT ["mantis"]
CMD ["--help"]
