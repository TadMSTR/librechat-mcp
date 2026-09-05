# syntax=docker/dockerfile:1

# python:3.13-slim, pinned by multi-arch index digest rather than by tag — the tag
# moves. Re-resolve with `docker buildx imagetools inspect python:3.13-slim` when
# bumping; do not copy the digest from a sibling repo, which will be pinned to a
# different snapshot. Resolved 2026-09-05.
#
# slim rather than alpine: under the [otel] extra the tree includes grpcio, which
# publishes manylinux wheels but whose musl coverage is the kind of thing that
# quietly turns into a build toolchain living in the runtime image.
ARG PYTHON_IMAGE=python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

FROM ${PYTHON_IMAGE} AS build

# Build into a self-contained venv so the runtime stage can take the tree wholesale
# without pip, setuptools, or any build metadata coming with it.
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src
# pyproject, README and LICENSE are all read by the setuptools build; copying just
# these plus the package keeps the layer cache from busting on a docs or test edit.
COPY pyproject.toml README.md LICENSE ./
COPY librechat_mcp ./librechat_mcp
# [otel] is installed deliberately, not incidentally. Setting
# OTEL_EXPORTER_OTLP_ENDPOINT without the extra present does not fail loudly — the
# exporter import fails once and then nothing is ever exported, which is a failure
# mode this fleet has already shipped into (vikunja#336). main() initialises OTEL
# eagerly, so the extra is required here rather than merely prudent.
RUN pip install --no-cache-dir '.[otel]'

FROM ${PYTHON_IMAGE} AS runtime

# MCP_PORT is baked because 8496 is this service's registered port on forge; the
# compose fragment repeats it so the choice stays visible where the stack is reviewed.
#
# Deliberately absent:
#   LIBRECHAT_URL              — a baked default of localhost:3080 is the container's
#                                OWN loopback, where nothing is listening. It must
#                                come from compose as http://librechat:3080, the
#                                service name on librechat-internal.
#   LIBRECHAT_ADMIN_EMAIL      — an account identity, not a build-time constant.
#   LIBRECHAT_ADMIN_PASSWORD   — never bake a credential into a layer.
#   LIBRECHAT_MCP_API_TOKEN    — likewise. main() refuses to start without one of at
#                                least 16 characters, so an image run with no token
#                                fails loudly instead of serving agent CRUD openly.
#   LOG_FILE                   — must stay unset. Logs go to stdout, which is what
#                                makes read_only: true viable with no writable path.
ENV MCP_PORT=8496 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

COPY --from=build /opt/venv /opt/venv

# Non-root, fixed uid/gid 1000 rather than a name lookup — matches the
# `user: "1000:1000"` the running service is already deployed with.
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --no-create-home app
USER 1000:1000

EXPOSE 8496

# /health is unauthenticated by design (exempted by EXACT path in
# _BearerAuthMiddleware) and returns a bare status, so this needs no token — which
# matters, because a HEALTHCHECK carrying the bearer token would put a credential in
# every `docker inspect`.
#
# It is a pure liveness check: it does not reach LibreChat, so a LibreChat restart
# does not mark this container unhealthy and trigger a pointless restart of a working
# process. This server is stateless and re-authenticates per request.
#
# python rather than wget/curl: slim ships neither, and adding one to the runtime
# image for a healthcheck is a worse trade than using the interpreter already here.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('MCP_PORT','8496')+'/health', timeout=4).status==200 else 1)" \
  || exit 1

CMD ["librechat-mcp"]
