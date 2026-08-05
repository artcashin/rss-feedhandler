IMAGE   ?= ghcr.io/artcashin/rss-ticker
TAG     ?= 8.0.0
BUILDER ?= rss-ticker-builder

.PHONY: test lint build buildx run

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

build:
	docker build -t $(IMAGE):$(TAG) -t $(IMAGE):latest .

buildx:
	@docker buildx inspect $(BUILDER) >/dev/null 2>&1 || \
	  docker buildx create --name $(BUILDER) --driver docker-container
	docker buildx build --builder $(BUILDER) --platform linux/amd64,linux/arm64 \
	  -t $(IMAGE):$(TAG) -t $(IMAGE):latest --push .

run:
	docker compose up -d --build
