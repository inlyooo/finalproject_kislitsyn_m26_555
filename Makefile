.PHONY: install run lint test build package-install

install:
	uv sync --frozen

run:
	uv run project

lint:
	uv run ruff check .
	uv run ruff format --check .

test:
	uv run python -m unittest discover -s tests -v

build:
	uv build

package-install: install build
	uv pip install --reinstall --no-deps --no-index --find-links dist finalproject-kislitsyn-m26-555
