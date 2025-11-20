#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -d "${ROOT_DIR}/venv" ]; then
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/venv/bin/activate"
fi

echo "Running isort..."
isort async_unzip tests

echo "Running flake8..."
flake8 async_unzip tests

echo "Running pylint..."
pylint async_unzip tests

echo "Running pytest..."
pytest -q

echo "All checks passed."
