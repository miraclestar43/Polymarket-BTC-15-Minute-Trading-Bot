#!/bin/sh
# wait_for_network.sh — fail-closed network/DNS readiness gate for the
# Polymarket DRY-RUN systemd service.
#
# Exits 0 only when BOTH are true within the timeout:
#   1) an internet route works (ping -c1 1.1.1.1)
#   2) DNS resolves clob.polymarket.com (getent hosts ...)
#
# Otherwise exits 1 (fail closed) so systemd does NOT start the runner on an
# unverified network. This script prints only generic status text — never any
# secrets, credentials, or .env contents.

set -eu

TIMEOUT_SECONDS=60
PING_TARGET="1.1.1.1"
DNS_TARGET="clob.polymarket.com"
SLEEP_BETWEEN=3

deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))

echo "wait_for_network: checking connectivity (timeout ${TIMEOUT_SECONDS}s)..."

while :; do
    route_ok=0
    dns_ok=0

    if ping -c1 -W2 "$PING_TARGET" >/dev/null 2>&1; then
        route_ok=1
    fi

    if getent hosts "$DNS_TARGET" >/dev/null 2>&1; then
        dns_ok=1
    fi

    if [ "$route_ok" -eq 1 ] && [ "$dns_ok" -eq 1 ]; then
        echo "wait_for_network: OK (internet route up, DNS resolves ${DNS_TARGET})"
        exit 0
    fi

    now=$(date +%s)
    if [ "$now" -ge "$deadline" ]; then
        echo "wait_for_network: FAIL (route_ok=${route_ok} dns_ok=${dns_ok}) after ${TIMEOUT_SECONDS}s — refusing to start (fail closed)" >&2
        exit 1
    fi

    echo "wait_for_network: not ready yet (route_ok=${route_ok} dns_ok=${dns_ok}); retrying in ${SLEEP_BETWEEN}s..."
    sleep "$SLEEP_BETWEEN"
done
