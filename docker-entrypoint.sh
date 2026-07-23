#!/bin/sh
# Container entrypoint: tick this ONE target (GANTRY_TARGET, set by
# gantry/docker.py::up to the SAME absolute path as on the host — see up()'s
# comment for why it can't be a fixed /workspace) forever. Unlike the host
# daemon (gantry/daemon.py), a container only ever serves one project — no
# target list, no launchd/systemd, Docker's own --restart policy replaces
# that layer.
set -e

INTERVAL="${GANTRY_TICK_INTERVAL:-60}"
# How often (in ticks) to sweep shipped/cancelled runs' worktrees + state —
# without this, worktrees (each a full checkout, e.g. a real node_modules
# for edupaid) accumulate forever and fill the disk. Confirmed live: hit a
# real ENOSPC mid-tick from exactly this. Every 30 ticks at the default
# 60s interval is ~30 minutes — frequent enough that a small boot disk
# doesn't have to survive hours of accumulation between deploys (which is
# the only other time cleanup used to run, via 03-deploy.sh's own prune
# step for Docker images specifically, not worktrees).
CLEANUP_EVERY_N_TICKS="${GANTRY_CLEANUP_EVERY_N_TICKS:-30}"

echo "gantry docker tick loop starting for $GANTRY_TARGET (interval ${INTERVAL}s, cleanup every ${CLEANUP_EVERY_N_TICKS} ticks)"

tick=0
while true; do
    gantry advance --all || echo "advance --all failed (exit $?), continuing"
    tick=$((tick + 1))
    if [ "$((tick % CLEANUP_EVERY_N_TICKS))" -eq 0 ]; then
        gantry cleanup --yes --purge-state --older-than-days 1 || echo "cleanup failed (exit $?), continuing"
    fi
    sleep "$INTERVAL"
done
