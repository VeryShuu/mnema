.PHONY: up down build test lint example login migrate

up:            ## podnyat' ves' stack (dev)
	docker compose up -d --build

down:          ## ostanovit'
	docker compose down

build:
	docker compose build

test:          ## pytest v local'nom venv
	./venv/bin/python -m pytest

example:       ## peresobrat' .env.example iz reestra (FT-37)
	./venv/bin/python -m mnema.core.registry > .env.example

login:         ## headful perelogin Zoom-sessii (pervyy raz i kazhdye ~2 nedeli)
	HEADFUL=1 docker compose run --rm web-bot

migrate:       ## primenit' alembic-migracii
	DATABASE_URL=sqlite:////data/db.sqlite ./venv/bin/alembic upgrade head
