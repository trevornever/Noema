# Self-host Noema with Docker Compose

This deployment is designed for a single machine and binds the web interface to
127.0.0.1 only. It uses the existing SQLite store on a named Docker volume;
PostgreSQL is a separate migration milestone and is not implied by this package.

## Requirements

- Docker Engine
- Docker Compose v2

## Start in mock mode

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps
```

Open http://localhost:8000. With no provider credentials, Noema uses its built-in
mock LLM and image providers. This is useful for validating the UI and branching
workflow without an API account.

## Optional local inference with Ollama

Start the Ollama service:

```bash
docker compose --profile local-ai up --build -d
docker compose exec ollama ollama pull qwen2.5:7b
```

The Compose defaults point Noema at Ollama's OpenAI-compatible endpoint. Model
downloads can be several gigabytes; choose a smaller model if the machine has
limited RAM. The Ollama service is not published on a host port.

To return to mock mode, remove or comment out the LLM settings in `.env` and
recreate Noema:

```bash
docker compose up -d --force-recreate noema
```

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
For remote access, put it behind a properly configured VPN/reverse proxy and add
authentication and per-user authorization before making it public.

Secrets belong in the untracked `.env` file. Do not commit it. Back up the
`noema_data` volume before upgrades or experiments.
