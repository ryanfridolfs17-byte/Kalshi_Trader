.DEFAULT_GOAL := all

.PHONY: all test backtest lint clean

test:
	python -m pytest tests/ -v

backtest:
	python scripts/backtest.py --config configs/poc.yaml --out artifacts/results.json

lint:
	-flake8 src/

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	rm -rf artifacts/

all: test backtest
