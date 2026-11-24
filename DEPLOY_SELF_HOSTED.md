# Self-host Noema with Docker Compose

This deployment is designed for a single machine and binds the web interface to
127.0.0.1 only. It uses the existing SQLite store on a named Docker volume;
PostgreSQL is a separate migration milestone and is not implied by this package.

## Requirements

- Docker Engine
- Docker Compose v2.24 or newer

## Start in mock mode

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps
```

Open http://localhost:8000. With no provider credentials, Noema uses its built-in
mock LLM and image providers. This validates the UI and branching workflow
without an external AI account.

## Optional local inference with Ollama

First enable the Ollama profile and pull a model:

```bash
docker compose --profile local-ai up -d ollama
docker compose exec ollama ollama pull qwen2.5:7b
```

Then add these values to `.env` and recreate Noema:

```dotenv
LLM_API_KEY=ollama
LLM_BASE_URL=http://ollama:11434/v1
LLM_MODEL=qwen2.5:7b
```

```bash
docker compose up --build -d noema
```

Model downloads can be several gigabytes; choose a smaller model if the machine
has limited RAM. Ollama is not published on a host port.

To return to mock mode, remove the three LLM settings from `.env` and recreate
Noema.

## Data and operations

- Session data persists in the `noema_data` named volume.
- `docker compose down` removes containers but preserves the volume.
- `docker compose down -v` **deletes all Noema and Ollama volume data**.
- Logs: `docker compose logs -f noema`
- Health: `curl http://127.0.0.1:8000/api/v1/health`

## Security boundary

This is a local single-user starter, not a hardened public deployment. The host
port is loopback-only and no authentication layer is included yet. Do not expose
port 8000 to the internet or place sensitive multi-user data on this instance.
Add authentication and per-user authorization before making it public.

Secrets belong in the untracked `.env` file. Do not commit it. Back up the
`noema_data` volume before upgrades or experiments.
