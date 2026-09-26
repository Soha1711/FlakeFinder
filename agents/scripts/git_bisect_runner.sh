#!/bin/bash

# Usage:
#   git_bisect_runner.sh <test_path::test_name> <N> <failure_threshold>
#
# Example:
#   git_bisect_runner.sh tests/test_d_regression.py::test_calculate_total 5 2

TEST_TARGET="$1"
N="${2:-5}"
FAILURE_THRESHOLD="${3:-1}"

if [ -z "$TEST_TARGET" ]; then
    echo "Usage: $0 <test_path::test_name> [N] [failure_threshold]"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEMO_REPO="$PROJECT_ROOT/demo-repo"

cd "$DEMO_REPO" || {
    echo "ERROR: Could not enter demo-repo: $DEMO_REPO"
    exit 1
}

echo "Starting git bisect for: $TEST_TARGET"
echo "Trials per commit: $N"
echo "Failures required to classify commit as bad: $FAILURE_THRESHOLD"

git bisect reset >/dev/null 2>&1 || true

START_COMMIT="$(git rev-parse 04805c1^)"
HEAD_COMMIT="$(git rev-parse HEAD)"

echo "Known good: $START_COMMIT"
echo "Current commit: $HEAD_COMMIT"

git bisect start "$HEAD_COMMIT" "$START_COMMIT"

while true; do
    CURRENT_COMMIT="$(git rev-parse HEAD)"
    FAIL_COUNT=0

    for ((i=1; i<=N; i++)); do
        if python -m pytest "$TEST_TARGET" -p no:randomly -q > /dev/null 2>&1; then
            :
        else
            FAIL_COUNT=$((FAIL_COUNT + 1))
        fi
    done

    if [ "$FAIL_COUNT" -ge "$FAILURE_THRESHOLD" ]; then
        echo "Commit $CURRENT_COMMIT: BAD ($FAIL_COUNT/$N failures)"
        git bisect bad
    else
        echo "Commit $CURRENT_COMMIT: GOOD ($FAIL_COUNT/$N failures)"
        git bisect good
    fi

    if git bisect log | grep -q "first bad commit"; then
        break
    fi
done

echo
echo "Bisect result:"
git bisect log | tail -n 8

git bisect reset >/dev/null 2>&1