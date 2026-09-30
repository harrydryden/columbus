# The US Outbound image (SPEC 3). On Railway it is the one always-on worker service: the
# default command is the scheduler (ops/scheduler.py), which starts every job as its own
# process on the schedule in ops/schedule.py. No port is exposed: there is no public
# endpoint (SPEC 2). Railway finds this file by its name and builds it; leave the
# service's custom start command empty so this ENTRYPOINT and CMD are used.
#   docker build -t us-outbound . && docker run --rm us-outbound schedule
#   one command in the running worker: railway ssh -- us-outbound status
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

RUN pip install . \
    && useradd --system --uid 10001 --home-dir /tmp --shell /usr/sbin/nologin us-outbound

USER 10001
# Exec form, so the scheduler is PID 1 and receives Railway's SIGTERM itself.
ENTRYPOINT ["us-outbound"]
CMD ["scheduler"]
