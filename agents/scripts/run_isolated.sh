#!/bin/bash

# Usage:
#   run_isolated.sh <test_path::test_name> <N>

TEST_TARGET="$1"
N="${2:-10}"

if [ -z "$TEST_TARGET" ]; then
    echo "Usage: $0 <test_path::test_name> [N]"
    exit 1
fi

# Find the FlakeFinder project root from this script's location.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEMO_REPO="$PROJECT_ROOT/demo-repo"

PASS_COUNT=0
FAIL_COUNT=0

cd "$DEMO_REPO" || {
    echo "ERROR: Could not enter demo-repo: $DEMO_REPO"
    exit 1
}

for ((i=1; i<=N; i++)); do
    if python -m pytest "$TEST_TARGET" -p no:randomly -q > /dev/null 2>&1; then
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
done

echo "{"
echo "  \"subagent\": \"isolation\","
echo "  \"test_target\": \"$TEST_TARGET\","
echo "  \"runs\": $N,"
echo "  \"passes\": $PASS_COUNT,"
echo "  \"failures\": $FAIL_COUNT"
echo "}"