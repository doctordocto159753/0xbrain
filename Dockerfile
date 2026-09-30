# syntax=docker/dockerfile:1
# 0xBrain runtime image. Code and data are NOT baked in: the instantiated
# repository (with .git) is bind-mounted at /wiki and the server runs from it
# (scripts/remote_mcp/server.py). The image carries only runtimes: Python, Git
# (+LFS hooks), Node + QMD (lexical search), the MCP SDK (remote transport +
# OAuth) and conversion libraries. No model weights, no STT/OCR/vision models,
# no embeddings, no remote-LLM SDK.

ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=22
ARG QMD_VERSION=2.8.3
# Overridable so a mirror or a base that already ships git can be used (e.g. python:3.12-bookworm).
ARG PYTHON_BASE=python:${PYTHON_VERSION}-slim-bookworm

FROM node:${NODE_VERSION}-bookworm-slim AS qmd
ARG QMD_VERSION
# Lexical search never loads llama.cpp: drop the GPU backends (~640 MB). CPU stubs stay so qmd still starts.
ARG PRUNE_GPU=1
# Optional TLS-inspecting-proxy CA: docker build --secret id=extra_ca,src=/path/ca-bundle.crt
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi; \
    npm install -g --no-audit --no-fund "@tobilu/qmd@${QMD_VERSION}" \
 && npm cache clean --force \
 && if [ "$PRUNE_GPU" = "1" ]; then \
      find /usr/local/lib/node_modules/@tobilu/qmd/node_modules/@node-llama-cpp -maxdepth 1 \
        \( -name '*cuda*' -o -name '*vulkan*' -o -name '*metal*' \) -exec rm -rf {} + ; \
    fi

FROM ${PYTHON_BASE} AS runtime
ARG WITH_CONVERSION=1
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git is required (K9 commits); git-lfs only silences the repository's LFS
# hooks and is installed whenever apt is reachable. A base that already ships
# git (python:3.12-bookworm) needs no package download at all.
RUN if ! command -v git >/dev/null 2>&1; then \
      apt-get update && apt-get install -y --no-install-recommends git git-lfs ca-certificates \
      && rm -rf /var/lib/apt/lists/*; \
    elif ! command -v git-lfs >/dev/null 2>&1; then \
      ( apt-get update && apt-get install -y --no-install-recommends git-lfs \
        && rm -rf /var/lib/apt/lists/* ) || echo "git-lfs unavailable; LFS hooks will only warn"; \
    fi \
 && git --version

# Node runtime + QMD, copied from the build stage (same Debian release => same glibc).
COPY --from=qmd /usr/local/bin/node /usr/local/bin/node
COPY --from=qmd /usr/local/lib/node_modules/@tobilu /usr/local/lib/node_modules/@tobilu
RUN ln -s ../lib/node_modules/@tobilu/qmd/bin/qmd /usr/local/bin/qmd \
 && qmd --version

COPY requirements.txt /tmp/requirements.txt
COPY requirements-remote.txt /tmp/requirements-remote.txt
COPY deploy/requirements-image.txt /tmp/requirements-image.txt
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install -r /tmp/requirements.txt -r /tmp/requirements-remote.txt \
 && if [ "$WITH_CONVERSION" = "1" ]; then pip install -r /tmp/requirements-image.txt; fi \
 && python -c "import mcp, uvicorn, yaml" \
 && rm -f /tmp/requirements*.txt

COPY deploy/entrypoint.sh /usr/local/bin/brain-entrypoint
COPY deploy/healthcheck.py /usr/local/lib/brain/healthcheck.py
COPY deploy/stub_server.py /usr/local/lib/brain/stub_server.py
RUN chmod 0755 /usr/local/bin/brain-entrypoint

# Arbitrary uid is supplied by compose (owner of the repo); nothing writes to $HOME.
ENV HOME=/state/home \
    WIKI_QMD_HOME=/state/qmd \
    XDG_CACHE_HOME=/state/qmd/cache \
    XDG_CONFIG_HOME=/state/qmd/config \
    BRAIN_HOST=0.0.0.0 \
    BRAIN_PORT=8080
WORKDIR /wiki
EXPOSE 8080
ENTRYPOINT ["/usr/bin/env", "brain-entrypoint"]
CMD ["serve"]
