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
TEST_PATH="tests/"
PARALLEL=false
CHANGED=false

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
            TEST_PATH="tests/unit_tests/"
            shift
            ;;
        -i|--integration-only)
            TEST_PATH="tests/integration/"
            shift
            ;;
        -p|--parallel)
            PARALLEL=true
            shift
            ;;
        --changed)
            CHANGED=true
            shift
            ;;
        --)
            shift
            break
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  -c, --coverage       Generate HTML coverage report"
            echo "  -v, --verbose        Increase verbosity"
            echo "  -u, --unit-only      Run only unit tests"
            echo "  -i, --integration-only Run only integration tests"
            echo "  -p, --parallel       Run tests with 2 workers, grouped by file"
            echo "  --changed            Select unit tests against UT_BASE_SHA (full UT on fallback)"
            echo "  --                   Pass remaining options to pytest"
            echo "  -h, --help           Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0                  # Run all tests"
            echo "  $0 -c               # Run tests with HTML coverage"
            echo "  $0 -u               # Run only unit tests"
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

if [[ "$CHANGED" == true && "$TEST_PATH" != "tests/unit_tests/" ]]; then
    echo "--changed requires --unit-only" >&2
    exit 2
fi

PYTEST_ARGS=("$VERBOSE" -p scripts.ut_schedule)
if [[ "$COVERAGE_REPORT" == "html" ]]; then
    PYTEST_ARGS+=(--cov=jiuwenswarm --cov-report=html --cov-report=term-missing)
fi
if [[ "$PARALLEL" == true ]]; then
    if python -c "import xdist" 2>/dev/null; then
        PYTEST_ARGS+=(-n 2 --dist=loadfile --max-worker-restart=0)
    else
        echo "pytest-xdist is unavailable. Running sequentially."
    fi
fi

TEST_PATHS=("$TEST_PATH")
if [[ "$CHANGED" == true ]]; then
    if SELECTION=$(python scripts/select_ut.py "${UT_BASE_SHA:-}") && [[ -n "$SELECTION" ]]; then
        TEST_PATHS=()
        while IFS= read -r path; do
            TEST_PATHS+=("$path")
        done <<< "$SELECTION"
    else
        echo "UT full suite: selector did not return test paths" >&2
    fi
fi

echo "Running test paths:"
printf '  %s\n' "${TEST_PATHS[@]}"
EXIT_CODE=0
python -u -m pytest "${PYTEST_ARGS[@]}" "$@" "${TEST_PATHS[@]}" || EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}  All tests passed!${NC}"
    echo -e "${GREEN}========================================${NC}"

    if [[ "$COVERAGE_REPORT" == "html" ]]; then
        echo ""
        echo -e "${YELLOW}Coverage report generated:${NC}"
        echo -e "  ${GREEN}file://$(pwd)/htmlcov/index.html${NC}"
    fi
else
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}  Some tests failed!${NC}"
    echo -e "${RED}========================================${NC}"
fi

exit $EXIT_CODE
