#!/usr/bin/env bash
# Diagnostic wrapper for the audio-distortion investigation.
#
# Usage:  bash tools/run_sampler.sh [duration_seconds]
# Default duration: 180s. Make your test call during that window.
#
# Pushes cpu_sampler.py + ws_sampler.py to every started machine in the
# clearslope-receptionist app, runs them in the background, then pulls
# the CSVs back to .tmp/cpu_samples/<utc-timestamp>/.
#
# Interpreting the output:
# * cpu CSV, peak `steal` >5% → Fly shared-cpu throttling (real)
#               peak `app_cpu_pct` >70% sustained → app is saturated
#               everything flat → CPU is not the issue
# * ws CSV, sustained `tx_q` >0 on a WS → app slow to drain frames
#               `retr` climbing during call  → network loss
#               everything flat → it's not transport-layer either, look
#               inside the audio handler itself.

set -euo pipefail

DURATION="${1:-180}"
APP="clearslope-receptionist"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$ROOT/.tmp/cpu_samples/$TS"
mkdir -p "$OUT"

if command -v flyctl >/dev/null 2>&1; then
    FLYCTL=flyctl
elif [ -x "$HOME/.fly/bin/flyctl" ]; then
    FLYCTL="$HOME/.fly/bin/flyctl"
else
    echo "flyctl not on PATH and not at ~/.fly/bin/flyctl" >&2
    exit 1
fi

mapfile -t MACHINES < <("$FLYCTL" machine list -a "$APP" --json | python3 -c '
import json, sys
for m in json.load(sys.stdin):
    if m.get("state") == "started":
        print(m["id"])
')

if [ ${#MACHINES[@]} -eq 0 ]; then
    echo "no started machines in app $APP" >&2
    exit 1
fi

CPU_B64=$(base64 -w0 < "$HERE/cpu_sampler.py")
WS_B64=$(base64 -w0 < "$HERE/ws_sampler.py")

REMOTE_SCRIPT=$(cat <<EOF
set -e
cd /tmp
echo "$CPU_B64" | base64 -d > cpu_sampler.py
echo "$WS_B64"  | base64 -d > ws_sampler.py
rm -f cpu_sample.csv cpu_sample.err ws_sample.csv ws_sample.err
DURATION=$DURATION nohup python3 -u cpu_sampler.py > cpu_sample.csv 2> cpu_sample.err < /dev/null &
DURATION=$DURATION nohup python3 -u ws_sampler.py  > ws_sample.csv  2> ws_sample.err  < /dev/null &
sleep 1
echo "started on \$(hostname)"
EOF
)
REMOTE_B64=$(printf '%s' "$REMOTE_SCRIPT" | base64 -w0)

echo "duration=${DURATION}s  machines=${MACHINES[*]}  output=$OUT"
for MID in "${MACHINES[@]}"; do
    "$FLYCTL" ssh console -a "$APP" --machine "$MID" \
        -C "sh -c \"echo $REMOTE_B64 | base64 -d | sh\"" 2>&1 | \
        grep -E "^started" || echo "WARN: no 'started' from $MID"
done

echo
echo ">>> Samplers running for ${DURATION}s, make the test call NOW <<<"
sleep "$((DURATION + 5))"

for MID in "${MACHINES[@]}"; do
    "$FLYCTL" ssh console -a "$APP" --machine "$MID" \
        -C "cat /tmp/cpu_sample.csv" 2>/dev/null > "$OUT/${MID}_cpu.csv" || true
    "$FLYCTL" ssh console -a "$APP" --machine "$MID" \
        -C "cat /tmp/ws_sample.csv" 2>/dev/null > "$OUT/${MID}_ws.csv" || true
done

echo
echo "done. files written:"
ls -la "$OUT"
