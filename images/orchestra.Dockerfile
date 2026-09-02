FROM docker@sha256:66d292e5c26bd33a6f6f61cacb880de2186339a524ecba1ce098dbbaceed6515 AS docker-cli

FROM python@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93 AS application

RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates git sqlite3; \
    rm -rf /var/lib/apt/lists/*; \
    pip install --no-cache-dir PyYAML==6.0.2; \
    groupadd --gid 1000 orchestra; \
    useradd --uid 1000 --gid 1000 --home-dir /var/lib/orchestra --shell /usr/sbin/nologin orchestra; \
    install -d -m 0750 -o orchestra -g orchestra /var/lib/orchestra /run/orchestra-docker /opt/orchestra/app

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --chown=orchestra:orchestra controller_api /opt/orchestra/app/controller_api
COPY --chown=orchestra:orchestra scripts /opt/orchestra/app/scripts
COPY --chown=orchestra:orchestra config /opt/orchestra/app/config
COPY --chown=orchestra:orchestra profiles /opt/orchestra/app/profiles
COPY --chown=orchestra:orchestra specs /opt/orchestra/app/specs
COPY --chown=orchestra:orchestra migrations /opt/orchestra/app/migrations
COPY --chown=orchestra:orchestra console/dist /opt/orchestra/app/console/dist
COPY --chown=orchestra:orchestra compose/images.lock.env /opt/orchestra/app/compose/images.lock.env

RUN rm -f \
        /opt/orchestra/app/scripts/check-secrets.py \
        /opt/orchestra/app/scripts/check-secrets.sh \
        /opt/orchestra/app/scripts/check-worker-oci-image.py \
        /opt/orchestra/app/scripts/init-test-fixtures.sh \
        /opt/orchestra/app/scripts/orchestra-compose.sh \
        /opt/orchestra/app/scripts/orchestra-console-build.py \
        /opt/orchestra/app/scripts/platform-support.sh \
        /opt/orchestra/app/scripts/verify-layout.sh \
        /opt/orchestra/app/config/host-packages.lock.toml; \
    ln -s /opt/orchestra/app /var/lib/orchestra/repo

ENV ORCHESTRA_ROOT=/var/lib/orchestra \
    ORCHESTRA_DATA_ROOT=/var/lib/orchestra \
    DOCKER_HOST=unix:///run/orchestra-docker/docker.sock \
    DOCKER_CONTEXT=default \
    DOCKER_CONFIG=/nonexistent/orchestra-empty-docker-config \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/orchestra/app:/opt/orchestra/app/scripts

USER orchestra:orchestra
WORKDIR /opt/orchestra/app
EXPOSE 8080
ENTRYPOINT ["python3", "/opt/orchestra/app/scripts/orchestra-appliance.py", "run"]
