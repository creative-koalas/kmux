#!/bin/bash

# Run unit tests
python -m pytest tests/unit/ -v

# Run coverage
python -m coverage run -m pytest tests/unit/
python -m coverage report
python -m coverage html

echo "Tests completed!"