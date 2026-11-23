# Self-host Noema

This is the first containerized release path. It runs the existing FastAPI + SQLite implementation with a named Docker volume, so data survives container replacement. PostgreSQL is a separate migration milestone; this Compose file does not pretend SQLite is PostgreSQL.

## Requirements

- Docker Engine
- Docker Compose v2
- 2 GB RAM for mock mode; more for local models

## Start (zero API keys)

    cp .env.example .env
    docker compose up --build -d
    docker compose ps

Open http://localhost:8000. Built-in mock providers work without external accounts or API keys.

## Optional local LLM with Ollama

    docker compose --profile local-ai up --build -d
    docker compose exec ollama ollama pull llama3.2

The example environment points the LLM adapter at Ollama's OpenAI-compatible endpoint. If Noema started before Ollama was enabled, recreate it after Ollama is ready:

    docker compose up -d --force-recreate noema

Model memory requirements vary. CPU-only execution may be slow; smaller quantized models are more practical on modest hardware.

## Operations

    docker compose logs -f noema
    docker compose restart noema
    docker compose down

The named noema_data volume is retained by docker compose down. To permanently erase local database and model-cache volumes, explicitly run:

    docker compose down -v

## Security boundaries

- API binds to host loopback (127.0.0.1), not LAN or internet.
- Container runs as non-root and drops Linux capabilities.
- Do not expose publicly until authentication, authorization, TLS, backups, and production controls are implemented.
- Keep .env private. Never commit provider keys.
- Back up noema_data before upgrades.

## Current scope

This release containerizes the existing SQLite store and provider adapters. It does not yet implement PostgreSQL, pgvector, multi-user authentication, or public internet deployment. Those are explicit follow-on milestones.
