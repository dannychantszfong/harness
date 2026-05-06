# Deployment Guide — Claude Agent Harness

**Version:** 2.0  
**Last updated:** 2026-05-03  

---

## Local Development (Standard Setup)

```bash
# 1. Create conda environment (Python 3.12)
conda create -n harness python=3.12 -y
conda activate harness

# 2. Install harness + extras
pip install -e .                    # core
pip install -e ".[sdk]"             # Claude Code SDK transport (optional)
pip install -e ".[dev]"             # pytest, pytest-asyncio, pytest-mock

# 3. Install one or both coding-agent CLIs:
#    Claude Code: https://claude.ai/download
#    Codex:       https://github.com/openai/codex

# 4. Set env vars only for the mode you intend to use
export ANTHROPIC_API_KEY=sk-ant-...   # Mode 2 (Claude API), or --with-api split
export OPENAI_API_KEY=sk-...          # Mode 4 (OpenAI API via Codex)
export GEMINI_API_KEY=AIza...         # Mode 5 (Gemini, via routing)
# Mode 6 (OpenRouter via Claude Code):
export ANTHROPIC_BASE_URL=https://openrouter.ai/api/v1
export ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY

# 5. Run tests (no live API/CLI calls — mocked)
pytest tests/ -v

# 6. Run against an example
harness run output/web_app_a3f8c21b/harness_config.json --runner subprocess
```

---

## Environment Variables Reference

| Variable | Mode | Description |
|----------|------|-------------|
| *(none)* | Modes 1, 3 (subscriptions) | Claude Code or Codex auths via your signed-in plan |
| `ANTHROPIC_API_KEY` | Mode 2 (Claude API) | Pay-per-token Claude Code via Anthropic API; also required for `orchestration_mode='api'` planner+evaluator |
| `OPENAI_API_KEY` | Mode 4 (OpenAI API) | Pay-per-token Codex via OpenAI API |
| `GEMINI_API_KEY` | Mode 5 (Gemini API) | Routed via Codex custom provider or via OpenRouter through Claude Code |
| `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` | Mode 6 (OpenRouter) | Point Claude Code at OpenRouter; token = `OPENROUTER_API_KEY` |

> **Note:** Modes 1 and 3 (subscriptions) need no API keys at all. The harness does not auto-export the env vars above — set them in your shell or `direnv`.

---

## Runner Installation Requirements

| Runner | Install command | Binary check |
|--------|----------------|-------------|
| `subprocess` | [Download Claude Code](https://claude.ai/download) | `which claude` |
| `sdk` | `pip install -e ".[sdk]"` | `python -c "import claude_code_sdk"` |
| `codex` | `npm install -g @openai/codex` | `which codex` |

---

## Running in Docker

```dockerfile
FROM continuumio/miniconda3:latest

WORKDIR /app
COPY . .

RUN conda create -n harness python=3.12 -y && \
    conda run -n harness pip install -e ".[sdk]"

# Install Claude Code CLI (or Codex CLI) inside the image — see
# https://claude.ai/download or https://github.com/openai/codex.

ENV ANTHROPIC_API_KEY=""
ENV OPENAI_API_KEY=""
ENV ANTHROPIC_BASE_URL=""
ENV ANTHROPIC_AUTH_TOKEN=""

ENTRYPOINT ["conda", "run", "-n", "harness", "harness"]
CMD ["--help"]
```

```bash
docker build -t claude-harness .

docker run \
  -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/output/my_project_a3f8c21b/harness_config.json:/app/output/my_project_a3f8c21b/harness_config.json \
  claude-harness run /app/output/my_project_a3f8c21b/harness_config.json --runner subprocess
```

> **Note:** All three runners require their coding-agent CLI inside the container. Subscription mode requires the user to be signed in to that CLI inside the image; for headless deployments, prefer Mode 2 (`ANTHROPIC_API_KEY`) or Mode 4 (`OPENAI_API_KEY`).

---

## Cost Planning by Mode

| Mode | Cost model | Typical cost per feature (5 iterations) |
|--------|-----------|----------------------------------------|
| 1 — Claude subscription | Pro / Max plan | ~$0 extra (counts against plan quota) |
| 3 — Codex subscription | OpenAI Plus | ~$0 extra (counts against plan quota) |
| 2 — Claude API | ~$15/$75 per 1M in/out (Opus 4.7) | ~$3–8 |
| 4 — OpenAI API | ~$5/$15 per 1M in/out (GPT-4o-class) | ~$1–4 |
| 5 — Gemini API | ~$1.25/$10 per 1M in/out (2.5 Pro) | ~$0.5–2 |
| 6 — OpenRouter API | Model-dependent | Varies |

> In `api` orchestration mode (`--with-api`), planner/evaluator additionally bill to `ANTHROPIC_API_KEY`. In default `runner` mode, that overhead stays inside whichever mode the generator is using.

---

## Running as a Background Job

```bash
conda activate harness

nohup harness run output/my_project_a3f8c21b/harness_config.json --runner subprocess \
  > harness.log 2>&1 &
echo $! > harness.pid

# Monitor
tail -f harness.log
watch -n 30 "harness status output/my_project_a3f8c21b/harness_config.json"

# Stop gracefully
kill -SIGINT $(cat harness.pid)
```

---

## Output Directory Layout

```
output/my_project/
├── features.json              # Source of truth: feature list + status + eval history
├── progress.md                # Human-readable summary (auto-updated)
├── init.sh                    # App startup script
├── handoff_session_0001.json  # Context reset handoffs
├── .git/                      # One commit per passing feature
├── src/                       # Generated application code (agentic runners)
│   ├── frontend/
│   └── backend/
└── ...
```

> All three runners produce real files in `src/` — they each drive a tool-using coding agent that writes to disk.

---

## Upgrading

```bash
conda activate harness
git pull
pip install -e ".[sdk]"  # if you use the SDK transport

# Existing features.json files remain compatible unless a Pydantic model field
# was renamed. Check harness/progress/models.py changelog before upgrading
# mid-project.
#
# Harness-owned project config lives in harness_config.json so imported repos
# can keep their own framework config files without collision.
#
# Generated/imported apps live under ignored output/ by default. Use
# --github-repo owner/repo or --git-remote URL to push each output project as
# its own independent repository.
```
