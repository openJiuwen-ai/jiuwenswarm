#!/bin/bash
# Test runner script for JiuwenClaw

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
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  -c, --coverage       Generate HTML coverage report"
            echo "  -v, --verbose        Increase verbosity"
            echo "  -u, --unit-only      Run only unit tests"
            echo "  -i, --integration-only Run only integration tests"
            echo "  -p, --parallel       Run tests in parallel (requires pytest-xdist)"
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

# Check if pytest is installed
if ! command -v pytest &> /dev/null; then
    echo -e "${RED}pytest is not installed!${NC}"
    echo "Install test dependencies:"
    echo "  pip install -e '.[test]'"
    exit 1
fi

# Build pytest command
PYTEST_CMD="pytest $VERBOSE"

# Add coverage
if [[ "$COVERAGE_REPORT" == "html" ]]; then
    echo -e "${YELLOW}Generating HTML coverage report...${NC}"
    PYTEST_CMD="$PYTEST_CMD --cov=jiuwenclaw --cov-report=html --cov-report=term-missing"
fi

# Add parallel execution
if [[ "$PARALLEL" == true ]]; then
    if ! python -c "import xdist" 2>/dev/null; then
        echo -e "${YELLOW}pytest-xdist not installed, running sequentially...${NC}"
        echo "Install it with: pip install pytest-xdist"
    else
        echo -e "${YELLOW}Running tests in parallel...${NC}"
        PYTEST_CMD="$PYTEST_CMD -n auto"
    fi
fi

# Add test path
PYTEST_CMD="$PYTEST_CMD $TEST_PATH"

# Print command
echo -e "${YELLOW}Running command:${NC}"
echo "$PYTEST_CMD"
echo ""

# Run tests
echo -e "${GREEN}Running tests...${NC}"
echo ""

eval $PYTEST_CMD
EXIT_CODE=$?

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
