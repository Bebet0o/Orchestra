# OCI-native successor to worker-sandbox.Dockerfile.  The inherited image stays
# in place until the published digest is activated in the S3-B transaction.
FROM python@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

ARG OCI_SOURCE="https://github.com/bebet0o/Orchestra"
ARG OCI_REVISION
ARG OCI_VERSION

LABEL org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.revision="${OCI_REVISION}" \
      org.opencontainers.image.version="${OCI_VERSION}" \
      org.opencontainers.image.base.name="docker.io/library/python" \
      org.opencontainers.image.base.digest="sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93"

RUN set -eux; \
    test -n "${OCI_REVISION}"; \
    test -n "${OCI_VERSION}"; \
    if command -v apt-get >/dev/null 2>&1; then \
        apt-get update; \
        DEBIAN_FRONTEND=noninteractive \
        apt-get install -y --no-install-recommends \
            bash \
            ca-certificates \
            coreutils \
            findutils \
            git \
            grep \
            procps \
            sed; \
        rm -rf /var/lib/apt/lists/*; \
    elif command -v apk >/dev/null 2>&1; then \
        apk add --no-cache \
            bash \
            ca-certificates \
            coreutils \
            findutils \
            git \
            grep \
            procps \
            sed; \
    else \
        echo "Unsupported package manager" >&2; \
        exit 1; \
    fi; \
    mkdir -p /home/orchestra /workspace; \
    chmod 1777 /home/orchestra /workspace

ENV HOME=/home/orchestra
WORKDIR /workspace
CMD ["sleep", "infinity"]
