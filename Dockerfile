# syntax=docker/dockerfile:1

# QwenPaw Fleet plugin manager.
#
# The image contains two things only: the Python package (stdlib only) and the
# Docker CLI. Discovery shells out to `docker ps/inspect`, so run the container
# with the host socket mounted:
#
#   docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
#     ghcr.io/ligandlly/qwenpaw-plugin-manager:latest plugins
#
# The static Docker CLI from the official image is a Go binary, so it runs
# unchanged on the Debian-based Python image below.

FROM docker:27-cli AS dockercli

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1

# ca-certificates: plugin sources may be https:// URLs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app
COPY qpm ./qpm

EXPOSE 8787

# Everything below runs without network at build time: no pip install, no
# wheels, no lockfile. The version is whatever is in qpm/__init__.py.
ENTRYPOINT ["python", "-m", "qpm"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8787"]
