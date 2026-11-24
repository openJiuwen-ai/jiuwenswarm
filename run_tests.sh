#!/bin/bash
# Test runner script for JiuwenSwarm

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  JiuwenClow Test Runner${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Default values
COVERAGE_REPORT="term"
VERBOSE="-v"
PARALLEL=false
SMART=false
FULL=false
SCOPE="all"
# pytest targets as an array (never word-split on filenames / eval them).
TEST_TARGETS=("tests/")

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -c|--coverage)
            COVERAGE_REPORT="html"
            shift
            ;;
        -v|--verbose)
            VERBOSE="-vv"
            shift
            ;;
        -u|--unit-only)
            SCOPE="unit"
            TEST_TARGETS=("tests/unit_tests/")
            shift
            ;;
        -i|--integration-only)
            SCOPE="integration"
            TEST_TARGETS=("tests/integration/")
            shift
            ;;
        -p|--parallel)
            PARALLEL=true
            shift
            ;;
        -s|--smart)
            SMART=true
            shift
            ;;
        -f|--full)
            FULL=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  -c, --coverage       Generate HTML coverage report"
            echo "  -v, --verbose        Increase verbosity"
            echo "  -u, --unit-only      Run only unit tests"
            echo "  -i, --integration-only Run only integration tests"
            echo "  -p, --parallel       Run tests in parallel (requires pytest-xdist)"
            echo "  -s, --smart          Run change-related tests + fixed baseline (scripts/select_tests.py)"
            echo "  -f, --full           Force full test run (overrides --smart)"
            echo "  -h, --help           Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0                  # Run all tests"
            echo "  $0 -c               # Run tests with HTML coverage"
            echo "  $0 -u               # Run only unit tests"
            echo "  $0 -u -s            # Smart: changed code + baseline unit tests"
            echo "  $0 -u -f            # Full unit tests"
            echo "  $0 -p               # Run tests in parallel"
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            echo "Use -h or --help for usage information"
            exit 1
            ;;
    esac
done

# Check if pytest is installed
if ! command -v pytest &> /dev/null; then
    echo -e "${RED}pytest is not installed!${NC}"
    echo "Install test dependencies:"
    echo "  pip install -e '.[test]'"
    exit 1
fi

# Resolve a Python interpreter once; used for the selector and xdist probe.
PYTHON_BIN=""
if command -v python3 &>/dev/null; then
    PYTHON_BIN=python3
elif command -v python &>/dev/null; then
    PYTHON_BIN=python
fi

# Build pytest command as an array (arguments are passed verbatim, no eval).
PYTEST_CMD=(pytest "$VERBOSE")

# Add coverage
if [[ "$COVERAGE_REPORT" == "html" ]]; then
    echo -e "${YELLOW}Generating HTML coverage report...${NC}"
    PYTEST_CMD+=(--cov=jiuwenswarm --cov-report=html --cov-report=term-missing)
fi

# Add parallel execution
if [[ "$PARALLEL" == true ]]; then
    if [[ -n "$PYTHON_BIN" ]] && $PYTHON_BIN -c "import xdist" 2>/dev/null; then
        echo -e "${YELLOW}Running tests in parallel...${NC}"
        PYTEST_CMD+=(-n auto)
    else
        echo -e "${YELLOW}pytest-xdist not installed, running sequentially...${NC}"
        echo "Install it with: pip install pytest-xdist"
    fi
fi

# Smart selection: replace the whole test path with change-related tests plus
# the fixed baseline. Falls back to the full scope when the change set is too
# broad (config/build files) or cannot be determined.
if [[ "$SMART" == true && "$FULL" != true ]]; then
    echo -e "${YELLOW}Selecting change-related tests (base ref: ${TEST_BASE_REF:-origin/develop}, scope: ${SCOPE})...${NC}"
    SELECTED=$($PYTHON_BIN scripts/select_tests.py --base "${TEST_BASE_REF:-origin/develop}" --scope "$SCOPE" 2>/dev/null || true)
    if [[ -z "$SELECTED" || "$SELECTED" == "FULL" ]]; then
        echo -e "${YELLOW}Change set too broad or unavailable; falling back to full ${SCOPE} scope${NC}"
    else
        TEST_TARGETS=()
        while IFS= read -r line; do
            if [[ -n "$line" ]]; then
                TEST_TARGETS+=("$line")
            fi
        done <<< "$SELECTED"
        echo -e "${GREEN}Selected tests:${NC}"
        printf '  %s\n' "${TEST_TARGETS[@]}"
    fi
fi

# Add test targets
PYTEST_CMD+=("${TEST_TARGETS[@]}")

# Print command
echo -e "${YELLOW}Running command:${NC}"
printf '  %s ' "${PYTEST_CMD[@]}"
echo ""
echo ""

# Run tests
echo -e "${GREEN}Running tests...${NC}"
echo ""

# set -e 会在此处直接退出脚本，导致下方失败横幅分支不可达；
# 先置零再用 || 捕获，保证 EXIT_CODE 拿到 pytest 退出码。
EXIT_CODE=0
"${PYTEST_CMD[@]}" || EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}  All tests passed! ✓${NC}"
    echo -e "${GREEN}========================================${NC}"

    if [[ "$COVERAGE_REPORT" == "html" ]]; then
        echo ""
        echo -e "${YELLOW}Coverage report generated:${NC}"
        echo -e "  ${GREEN}file://$(pwd)/htmlcov/index.html${NC}"
    fi
else
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}  Some tests failed! ✗${NC}"
    echo -e "${RED}========================================${NC}"
fi

exit $EXIT_CODE
