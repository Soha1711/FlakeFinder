#!/bin/bash

# Usage:
#   run_shuffled.sh <test_path::test_name> <N>

TEST_NODE="$1"
N="${2:-10}"

if [ -z "$TEST_NODE" ]; then
    echo "Usage: $0 <test_path::test_name> [N]"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEMO_REPO="$PROJECT_ROOT/demo-repo"

PASS_COUNT=0
FAIL_COUNT=0
FAILING_RUNS=()

cd "$DEMO_REPO" || {
    echo "ERROR: Could not enter demo-repo: $DEMO_REPO"
    exit 1
}

for ((i=1; i<=N; i++)); do

    SEED=$RANDOM
    XML_FILE="shuffle_${SEED}.xml"

    python -m pytest -q \
        --randomly-seed="$SEED" \
        --junitxml="$XML_FILE" \
        > /dev/null 2>&1

    RESULT=$(python - "$XML_FILE" "$TEST_NODE" <<'PY'
import sys
import xml.etree.ElementTree as ET

xml_file = sys.argv[1]
target = sys.argv[2]

target_file, target_test = target.split("::", 1)
target_module = target_file.replace("\\", "/").replace("/", ".")
target_module = target_module[:-3] if target_module.endswith(".py") else target_module

tree = ET.parse(xml_file)
root = tree.getroot()

testcases = list(root.iter("testcase"))

target_index = None
target_result = None

for index, testcase in enumerate(testcases):
    classname = testcase.attrib.get("classname", "")
    name = testcase.attrib.get("name", "")

    if classname == target_module and name == target_test:
        target_index = index

        if testcase.find("failure") is not None or testcase.find("error") is not None:
            target_result = "FAIL"
        else:
            target_result = "PASS"

        break

if target_index is None:
    print("UNKNOWN")
else:
    previous_test = None

    if target_index > 0:
        previous = testcases[target_index - 1]
        previous_test = (
            previous.attrib.get("classname", "")
            + "::"
            + previous.attrib.get("name", "")
        )

    print(target_result)
    print(previous_test if previous_test else "NONE")
PY
)

    TARGET_RESULT=$(echo "$RESULT" | sed -n '1p')
    PREVIOUS_TEST=$(echo "$RESULT" | sed -n '2p')

    if [ "$TARGET_RESULT" = "PASS" ]; then
        PASS_COUNT=$((PASS_COUNT + 1))

    elif [ "$TARGET_RESULT" = "FAIL" ]; then
        FAIL_COUNT=$((FAIL_COUNT + 1))

        FAILING_RUNS+=(
            "{\"seed\": $SEED, \"previous_test\": \"$PREVIOUS_TEST\"}"
        )

    else
        echo "WARNING: Could not determine result for $TEST_NODE with seed $SEED" >&2
    fi

    rm -f "$XML_FILE"

done

echo "{"
echo "  \"subagent\": \"shuffle\","
echo "  \"test_target\": \"$TEST_NODE\","
echo "  \"runs\": $N,"
echo "  \"passes\": $PASS_COUNT,"
echo "  \"failures\": $FAIL_COUNT,"
echo "  \"failing_runs\": ["

if [ ${#FAILING_RUNS[@]} -gt 0 ]; then
    printf "    %s" "${FAILING_RUNS[0]}"

    for ((j=1; j<${#FAILING_RUNS[@]}; j++)); do
        printf ",\n    %s" "${FAILING_RUNS[$j]}"
    done

    echo
fi

echo "  ]"
echo "}"