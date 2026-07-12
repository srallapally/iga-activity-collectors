#!/usr/bin/env bash
# deployment/docker-run.sh
#
# Example one-shot invocation of the iga-collectors image, plus a cron
# entry to schedule it. The container does ONE run and exits (matches
# __main__.py's design) -- there is no "keep the container running"
# option here; scheduling happens outside the container.

set -euo pipefail

docker run --rm \
  --env-file /etc/iga-collectors/iga.env \
  -v /etc/iga-collectors/collectors:/collectors:ro \
  -v /var/lib/iga-collectors/state:/state \
  -v /etc/iga-collectors/jdbc-drivers:/jdbc-drivers:ro \
  -e COLLECTORS_DIR=/collectors \
  -e CHECKPOINT_STORE_PATH=/state/checkpoints.json \
  iga-collectors:latest

# ---------------------------------------------------------------------------
# /etc/iga-collectors/iga.env  (chmod 600, NOT committed to any repo)
#
#   IGA_PROTOCOL=https
#   IGA_HOST=openam-xxx.forgeblocks.com
#   IGA_PORT=443
#   IGA_UPLOAD_PATH=/iga/governance/activity
#   IGA_TOKEN_URL=https://openam-xxx.forgeblocks.com/oauth2/access_token
#   IGA_CLIENT_ID=...
#   IGA_CLIENT_SECRET=...
#   IGA_OAUTH_SCOPE=...
#
# Using --env-file instead of -e for these on purpose: -e values are
# visible in `docker inspect` and the host process list; --env-file at
# least keeps them out of shell history and ps output. Neither is a
# substitute for a real secrets manager if you have one available.
#
# /etc/iga-collectors/collectors/  -- your collector .py files and their
# sibling .json configs (see discovery.py's per-collector config
# convention). Mounted read-only: the container has no reason to write
# to it.
#
# /var/lib/iga-collectors/state/  -- NOT read-only. This is where
# CHECKPOINT_STORE_PATH lives. Must persist across container runs -- see
# the Dockerfile's own comment on this; it's a correctness requirement,
# not a nice-to-have.
# ---------------------------------------------------------------------------

# /etc/cron.d/iga-collectors  -- run every 15 minutes as root
#
#   */15 * * * * root /usr/local/bin/docker-run.sh >> /var/log/iga-collectors.log 2>&1
#
# Exit code from the container (see __main__.py):
#   0 = every discovered collector succeeded
#   1 = fatal error before any collector could run (bad config, etc.)
#   2 = ran, but at least one collector failed -- check the log for which
# cron itself doesn't distinguish these; if you want alerting on exit
# code 2 specifically (vs. 1), check $? in this script and wire it to
# whatever monitoring you already have (a dead-man's-switch ping, a
# Slack webhook, etc.) -- not included here since that depends entirely
# on what you're already using.