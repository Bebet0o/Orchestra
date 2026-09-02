FROM docker@sha256:66d292e5c26bd33a6f6f61cacb880de2186339a524ecba1ce098dbbaceed6515

COPY scripts/orchestra-runtime-entrypoint.sh /usr/local/bin/orchestra-runtime-entrypoint

RUN chmod 0755 /usr/local/bin/orchestra-runtime-entrypoint

ENV DOCKER_TLS_CERTDIR="" \
    DOCKER_HOST=unix:///run/orchestra-docker/docker.sock \
    ORCHESTRA_DATA_ROOT=/var/lib/orchestra

ENTRYPOINT ["/usr/local/bin/orchestra-runtime-entrypoint"]
CMD ["run"]
