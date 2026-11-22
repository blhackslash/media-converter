#!/bin/sh
# Runs the conversion, then the integrity check.
#
# Each step runs in the background and the shell waits for it, so that a
# SIGTERM from `docker stop` (or Ctrl+C) can be forwarded to the Python
# process. It then stops its encoders and removes unfinished files instead of
# being killed hard after the stop timeout.
set -u

child=""
forward() {
    [ -n "$child" ] && kill -s "$1" "$child" 2>/dev/null
}
trap 'forward TERM' TERM
trap 'forward INT' INT

run() {
    "$@" &
    child=$!
    # `wait` returns early when a trapped signal arrives; keep waiting until
    # the child has really exited and collect its exit code.
    while :; do
        wait "$child"
        status=$?
        kill -0 "$child" 2>/dev/null || break
    done
    child=""
    return "$status"
}

echo "Starting Pipeline..."
run python3 /app/converter.py || exit $?

echo "Starting Integrity Check..."
run python3 /app/check.py || exit $?

echo "Pipeline Finished Successfully."
