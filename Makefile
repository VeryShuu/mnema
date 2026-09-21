.PHONY: up down build test lint example login migrate gpu-up gpu-build gpu-down gpu-logs

COMPOSE_GPU = docker compose -f docker-compose.yml -f docker-compose.gpu.yml

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

# --- GPU-versiya (worker s CUDA; nuzhen nvidia-container-toolkit) --------------
gpu-build:     ## sobrat' GPU-obraz workera
	$(COMPOSE_GPU) build worker

gpu-up:        ## podnyat' stack, worker na GPU
	$(COMPOSE_GPU) up -d --build

gpu-down:      ## ostanovit' GPU-stack
	$(COMPOSE_GPU) down

gpu-logs:      ## logi GPU-workera
	$(COMPOSE_GPU) logs -f worker
