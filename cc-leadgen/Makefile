.PHONY: up down logs ps migrate worker beat test lint clean

# ── Docker ──────────────────────────────────────────────────────────────────
up: docker-compose.yml
	docker compose up --build -d
	@echo "✅ Services up. App: http://localhost:8000"

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

# ── Database ────────────────────────────────────────────────────────────────
migrate:
	docker compose exec app alembic upgrade head

migrate-create:
	docker compose exec app alembic revision --autogenerate -m "$(MSG)"

# ── Workers ─────────────────────────────────────────────────────────────────
worker:
	docker compose up -d worker

beat:
	docker compose up -d beat

# ── Development ─────────────────────────────────────────────────────────────
test:
	docker compose exec app pytest

lint:
	docker compose exec app ruff check .

clean:
	docker compose down -v --remove-orphans
	rm -rf __pycache__ .pytest_cache