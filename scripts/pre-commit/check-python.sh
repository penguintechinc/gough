#!/bin/bash
# Python Pre-Commit Checks
# Runs linting and security checks for Python code

set -e

PROJECT_ROOT="${1:-.}"
PYTHON_DIRS=()

echo "Python Pre-Commit Checks"
echo "========================"
echo "Project: ${PROJECT_ROOT}"
echo ""

# Directories to exclude from linting (templates, third-party code)
EXCLUDE_PATTERNS=(
    "app-skeleton"
    "gough"
    "__pycache__"
    ".venv"
    "venv"
    "node_modules"
)

# Build exclude args for flake8
FLAKE8_EXCLUDE=""
for pattern in "${EXCLUDE_PATTERNS[@]}"; do
    if [ -n "$FLAKE8_EXCLUDE" ]; then
        FLAKE8_EXCLUDE="${FLAKE8_EXCLUDE},*/${pattern}/*"
    else
        FLAKE8_EXCLUDE="*/${pattern}/*"
    fi
done

# Find Python directories
if [ -d "$PROJECT_ROOT/services/flask-backend" ]; then
    PYTHON_DIRS+=("$PROJECT_ROOT/services/flask-backend")
fi

if [ -d "$PROJECT_ROOT/services/access-agent" ]; then
    PYTHON_DIRS+=("$PROJECT_ROOT/services/access-agent")
fi

# Find any other directories with Python files (excluding templates)
while IFS= read -r -d '' dir; do
    # Skip excluded patterns
    skip=false
    for pattern in "${EXCLUDE_PATTERNS[@]}"; do
        if [[ "$dir" == *"$pattern"* ]]; then
            skip=true
            break
        fi
    done
    if [ "$skip" = false ]; then
        PYTHON_DIRS+=("$dir")
    fi
done < <(find "$PROJECT_ROOT" -name "*.py" -type f -exec dirname {} \; | sort -u | tr '\n' '\0')

if [ ${#PYTHON_DIRS[@]} -eq 0 ]; then
    echo "No Python files found. Skipping."
    exit 0
fi

echo "Found Python directories:"
printf '%s\n' "${PYTHON_DIRS[@]}"
echo ""

FAILED=0

# Linting
echo "--- Linting ---"

# flake8
if command -v flake8 &> /dev/null; then
    echo "Running flake8..."
    for dir in "${PYTHON_DIRS[@]}"; do
        if ! flake8 "$dir" --max-line-length=120 --ignore=E501,W503 --exclude="*app-skeleton*,*gough*,*__pycache__*,*.venv*,*venv*"; then
            echo "flake8 failed for $dir"
            ((FAILED++))
        fi
    done
else
    echo "flake8 not installed, skipping"
fi

# black (check mode)
if command -v black &> /dev/null; then
    echo "Running black --check..."
    for dir in "${PYTHON_DIRS[@]}"; do
        if ! black --check --quiet "$dir" 2>/dev/null; then
            echo "black format check failed for $dir"
            ((FAILED++))
        fi
    done
else
    echo "black not installed, skipping"
fi

# isort (check mode)
if command -v isort &> /dev/null; then
    echo "Running isort --check..."
    for dir in "${PYTHON_DIRS[@]}"; do
        if ! isort --check-only --quiet "$dir" 2>/dev/null; then
            echo "isort check failed for $dir"
            ((FAILED++))
        fi
    done
else
    echo "isort not installed, skipping"
fi

# mypy
if command -v mypy &> /dev/null; then
    echo "Running mypy..."
    for dir in "${PYTHON_DIRS[@]}"; do
        if ! mypy "$dir" --ignore-missing-imports --no-error-summary 2>/dev/null; then
            echo "mypy found type errors in $dir"
            ((FAILED++))
        fi
    done
else
    echo "mypy not installed, skipping"
fi

echo ""
echo "--- Security ---"

# bandit
if command -v bandit &> /dev/null; then
    echo "Running bandit..."
    for dir in "${PYTHON_DIRS[@]}"; do
        if ! bandit -r "$dir" -ll -q 2>/dev/null; then
            echo "bandit found security issues in $dir"
            ((FAILED++))
        fi
    done
else
    echo "bandit not installed, skipping"
fi

# safety (if requirements.txt exists)
if command -v safety &> /dev/null; then
    echo "Running safety check..."
    for dir in "${PYTHON_DIRS[@]}"; do
        if [ -f "$dir/requirements.txt" ]; then
            if ! safety check -r "$dir/requirements.txt" --short-report 2>/dev/null; then
                echo "safety found vulnerable dependencies in $dir"
                ((FAILED++))
            fi
        fi
    done
else
    echo "safety not installed, skipping"
fi

echo ""
echo "--- Build Check ---"

# Syntax check
echo "Running syntax check..."
find "$PROJECT_ROOT" -name "*.py" -type f ! -path "*/.venv/*" ! -path "*/venv/*" ! -path "*/__pycache__/*" | while read -r pyfile; do
    if ! python3 -m py_compile "$pyfile" 2>/dev/null; then
        echo "Syntax error in $pyfile"
        ((FAILED++))
    fi
done

echo ""
if [ "$FAILED" -eq 0 ]; then
    echo "All Python checks passed!"
    exit 0
else
    echo "Python checks failed: $FAILED issues found"
    exit 1
fi
