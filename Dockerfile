# The US Outbound jobs image (SPEC 3): one image, one Cloud Run Job per job, chosen by args.
#   docker build -t us-outbound . && docker run --rm us-outbound status
FROM python:3.12-slim

# PYTHONPATH puts the source tree first, so sql/ and templates/ are found beside the package.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    HOME=/tmp

WORKDIR /app
COPY pyproject.toml README.md SPEC.md ./
COPY us_outbound ./us_outbound
COPY sql ./sql
COPY templates ./templates
COPY deploy/jobs.yaml ./deploy/jobs.yaml

RUN pip install . \
    && useradd --system --uid 10001 --home-dir /tmp --shell /usr/sbin/nologin us-outbound

USER 10001
ENTRYPOINT ["us-outbound"]
CMD ["status"]
