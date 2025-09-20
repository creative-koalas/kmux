#!/bin/bash

# Run integration tests (real zsh sessions)
echo "Running integration tests with real zsh sessions..."
python -m pytest tests/integration/ -v -x

# Run unit tests
echo "Running unit tests..."
python -m pytest tests/unit/ -v

# Run coverage
echo "Running coverage..."
python -m coverage run -m pytest tests/
python -m coverage report
python -m coverage html

echo "All tests completed!"