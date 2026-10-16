# 🐳 Running BIG QA AI Solution with Docker

This guide explains how to run the BIG QA AI Solution as a Docker container so any user can get it working with a single command — no Python, Chrome, or JMeter installation required.

---

## Prerequisites

| Requirement | Version | Install |
|---|---|---|
| Docker Desktop | 4.x or later | https://www.docker.com/products/docker-desktop/ |
| Docker Compose | Included in Docker Desktop | — |

> **Note:** On Windows, make sure Docker Desktop is running and **WSL 2 backend** is enabled (recommended).

---

## Quick Start

### Step 1 — Clone the repository

```bash
git clone https://github.com/your-org/BIG-QA-AI-Solution.git
cd BIG-QA-AI-Solution/BIG_QA_Solution
```

### Step 2 — Configure your API keys

Copy the example environment file and fill in your values:

```bash
# Windows (PowerShell)
Copy-Item .env.example ScriptGenerator\.env

# Linux / macOS
cp .env.example ScriptGenerator/.env
```

Edit `ScriptGenerator/.env`:

```env
AI_TOOL=GEMINI
AI_MODEL=gemini-2.5-flash-lite
API_KEY=your-actual-gemini-api-key
AI_PROVIDER=gemini
LOCAL_DB_NAME=local_database
CRED_ENC_KEY=your-encryption-key
FLASK_SECRET_KEY=your-flask-secret-key
APP_TIMEZONE=America/New_York
```

> ⚠️ **Never commit your real `.env` file to Git.** It is already listed in `.gitignore`.

### Step 3 — Start the container

```bash
docker-compose up --build
```

The first build takes **5–10 minutes** (it downloads Chrome + JMeter). Subsequent starts are instant.

### Step 4 — Open the app

Navigate to **http://localhost:5000** and log in:

| Field | Value |
|---|---|
| Email | `admin@big.com` |
| Password | `admin123` |

> ℹ️ Change the admin password after your first login via **Admin → Users**.

---

## Where are my projects stored?

Generated test projects are stored in the `./workspace` folder **on your local machine** (next to `docker-compose.yml`). The container writes to `/workspace` internally, which is bind-mounted to your local `./workspace` folder.

```
Your machine:              Inside the container:
./workspace/  ←──────────→  /workspace/
```

When you create a performance project and set its **Project Path** to something like `/workspace/MyApp`, the generated files will appear at `./workspace/MyApp` on your Windows machine immediately — accessible from File Explorer, VS Code, or any tool.

You can change the host location by setting `WORKSPACE_PATH` in your environment:

```bash
# Windows PowerShell
$env:WORKSPACE_PATH="C:\Rakesh\MyProjects"; docker-compose up
```

Or add it to your shell profile / a `.env` file next to `docker-compose.yml`.

---

## Data Persistence

| Data | Where it lives | How it persists |
|---|---|---|
| Users, projects, DB | Docker volume `db_data` | ✅ Survives container restarts and rebuilds |
| Generated test projects | Your host `./workspace` | ✅ Local files — always there |
| Chrome cache | Docker volume `chrome_cache` | ✅ Speeds up Chrome start after first use |
| API keys / secrets | `ScriptGenerator/.env` on host | ✅ Mounted read-only; never in the image |

---

## Running in the Background (detached)

```bash
docker-compose up -d
```

View logs:

```bash
docker-compose logs -f bigqa
```

Stop:

```bash
docker-compose down
```

---

## Pulling from Docker Hub

Once the image is published you can pull it directly without building:

```bash
docker pull your-dockerhub-username/bigqa-solution:latest
docker-compose up
```

---

## Publishing the Image to Docker Hub

```bash
# Log in to Docker Hub
docker login

# Build and tag
docker build -t your-dockerhub-username/bigqa-solution:latest .

# Push
docker push your-dockerhub-username/bigqa-solution:latest
```

---

## Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `AI_TOOL` | — | AI provider (`GEMINI`, `openai`, `anthropic`) |
| `AI_MODEL` | — | Model name (e.g. `gemini-2.5-flash-lite`) |
| `API_KEY` | — | API key for the chosen provider |
| `AI_PROVIDER` | — | Provider alias (`gemini`, `openai`, `anthropic`) |
| `FLASK_SECRET_KEY` | — | Flask session signing key (generate with `openssl rand -hex 32`) |
| `CRED_ENC_KEY` | — | Key used to encrypt stored credentials |
| `APP_TIMEZONE` | `America/New_York` | Timezone for timestamps (e.g. `Asia/Kolkata`, `Europe/London`) |
| `WORKSPACE_PATH` | `./workspace` | Host path mapped to `/workspace` inside the container |
| `DOCKER` | `true` | Set automatically — signals headless Chrome mode |
| `HEADLESS` | `true` | Set automatically — skips browser auto-open on startup |

---

## Troubleshooting

### Container exits immediately

Check the logs:
```bash
docker-compose logs bigqa
```
Most common cause: `ScriptGenerator/.env` is missing or has empty `API_KEY`.

### Chrome crashes inside the container

Ensure `shm_size: "2gb"` is set in `docker-compose.yml` (it is, by default). If you still see `SIGBUS` or shared memory errors, add `--disable-dev-shm-usage` to your Chrome args — this is already done automatically by the recorder when `DOCKER=true`.

### Port 5000 already in use

Change the host port in `docker-compose.yml`:
```yaml
ports:
  - "5001:5000"   # use localhost:5001 instead
```

### JMeter not found

JMeter is at `/opt/jmeter/bin/jmeter` inside the container. Verify with:
```bash
docker exec bigqa jmeter --version
```

---

## Architecture Notes

- **Flask** (port 5000) serves the web UI and all API endpoints.
- **FastAPI / uvicorn** (port 8000) handles AI script generation as a background subprocess.
- **Selenium + Chrome** is used by the Performance Recorder. In Docker, Chrome runs headlessly with `--no-sandbox --disable-dev-shm-usage --headless=new` applied automatically.
- **SQLite** is the database — lightweight and file-based. The file is stored in a named Docker volume so it is never lost on container rebuild.
- **JMeter** is at `/opt/jmeter`; the app calls it via subprocess when a JMeter performance project is run.
