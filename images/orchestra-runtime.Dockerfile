FROM docker@sha256:66d292e5c26bd33a6f6f61cacb880de2186339a524ecba1ce098dbbaceed6515

ARG OCI_SOURCE="https://github.com/bebet0o/Orchestra"
ARG OCI_REVISION
ARG OCI_VERSION

LABEL org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.revision="${OCI_REVISION}" \
      org.opencontainers.image.version="${OCI_VERSION}" \
      org.opencontainers.image.title="Orchestra Runtime"

COPY scripts/orchestra-runtime-entrypoint.sh /usr/local/bin/orchestra-runtime-entrypoint

RUN set -eux; \
    test -n "${OCI_REVISION}"; \
    test -n "${OCI_VERSION}"; \
    chmod 0755 /usr/local/bin/orchestra-runtime-entrypoint

ENV DOCKER_TLS_CERTDIR="" \
    DOCKER_HOST=unix:///run/orchestra-docker/docker.sock \
    ORCHESTRA_DATA_ROOT=/var/lib/orchestra

ENTRYPOINT ["/usr/local/bin/orchestra-runtime-entrypoint"]
CMD ["run"]
