#!/bin/sh
# Container entrypoint: tick this ONE target (GANTRY_TARGET, set by
# gantry/docker.py::up to the SAME absolute path as on the host — see up()'s
# comment for why it can't be a fixed /workspace) forever. Unlike the host
# daemon (gantry/daemon.py), a container only ever serves one project — no
# target list, no launchd/systemd, Docker's own --restart policy replaces
# that layer.
set -e

INTERVAL="${GANTRY_TICK_INTERVAL:-60}"

echo "gantry docker tick loop starting for $GANTRY_TARGET (interval ${INTERVAL}s)"

while true; do
    gantry advance --all || echo "advance --all failed (exit $?), continuing"
    sleep "$INTERVAL"
done
