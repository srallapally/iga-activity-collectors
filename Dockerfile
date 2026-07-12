# Dockerfile
#
# Solves the one thing pip/pipx can't: the JDBC collector needs a JVM
# (JayDeBeApi wraps the driver via JPype) and there's no pure-Python JDBC
# client. confluent-kafka's prebuilt wheels bundle librdkafka for the
# common linux/amd64 and linux/arm64 manylinux targets, so no extra system
# package is needed for that one on those platforms -- see the comment
# below if you're building for a platform without a prebuilt wheel.
#
# This image runs the CLI, does one run, and exits -- it is NOT a
# long-running daemon (matches src/iga_collectors/__main__.py's design).
# Schedule it with host cron calling `docker run`, a Kubernetes CronJob,
# or equivalent; this Dockerfile doesn't include a scheduler.

FROM python:3.12-slim

# JDBC collector dependency. Comment out if you never enable that
# collector -- default-jre-headless adds real image size for a JVM you
# may not need.
RUN apt-get update && \
    apt-get install -y --no-install-recommends default-jre-headless && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

# --extra-index or platform without a confluent-kafka prebuilt wheel:
# add `librdkafka-dev gcc` to the apt-get install above before this step,
# or this pip install will fail trying to build confluent-kafka from
# source with no librdkafka headers available.
RUN pip install --no-cache-dir ".[jdbc,kafka,google,aws]"

# --- Everything below is customer-provided at runtime, not baked in ---
#
# COLLECTORS_DIR: mount your collector .py files (and their sibling
# .json configs) as a volume, e.g.:
#   -v /host/path/to/collectors:/collectors -e COLLECTORS_DIR=/collectors
#
# CHECKPOINT_STORE_PATH: MUST be a mounted volume, not left on the
# container's ephemeral filesystem. If it isn't, every container restart
# loses all checkpoint state, every collector treats the next run as its
# first run, and -- depending on each collector's initial_lookback_seconds
# -- can re-upload events already uploaded. This is a correctness
# concern, not a convenience:
#   -v /host/path/to/state:/state -e CHECKPOINT_STORE_PATH=/state/checkpoints.json
#
# JDBC driver jars: no specific vendor driver is bundled (it's
# customer/database-specific). Mount your driver .jar(s) into a directory
# and point your jdbc_collector.py config's jdbc_driver_jars at it:
#   -v /host/path/to/jdbc-drivers:/jdbc-drivers

ENTRYPOINT ["iga-collectors"]