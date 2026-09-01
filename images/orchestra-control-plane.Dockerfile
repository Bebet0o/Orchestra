FROM docker@sha256:66d292e5c26bd33a6f6f61cacb880de2186339a524ecba1ce098dbbaceed6515 AS docker-cli

FROM python@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        sqlite3; \
    rm -rf /var/lib/apt/lists/*; \
    pip install --no-cache-dir PyYAML==6.0.2

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY . /opt/orchestra/repo

ENV ORCHESTRA_ROOT=/opt/orchestra \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/orchestra/repo:/opt/orchestra/repo/scripts

WORKDIR /opt/orchestra/repo

