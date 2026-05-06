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
pip install -e .                    # core (Anthropic API always included)
pip install -e ".[sdk]"            # Claude Code SDK runner
pip install -e ".[openai]"         # OpenAI + OpenRouter runners
pip install -e ".[gemini]"         # Google Gemini runner
pip install -e ".[dev]"            # pytest, pytest-asyncio, pytest-mock

# 3. Set API keys
export ANTHROPIC_API_KEY=sk-ant-...   # api orchestration or anthropic runner
export OPENAI_API_KEY=sk-...          # if using openai / openrouter runner
export GEMINI_API_KEY=AIza...         # if using gemini runner
export OPENROUTER_API_KEY=sk-or-...   # if using openrouter runner

# 4. Run tests (no API keys needed — mocked)
pytest tests/ -v

# 5. Run against an example
harness run examples/web_app.yaml --runner subprocess
```

---

## Environment Variables Reference

| Variable | Required | Used by |
|----------|----------|---------|
| `ANTHROPIC_API_KEY` | `orchestration_mode: api` or `anthropic` runner | Planner, Evaluator, `anthropic` runner |
| `OPENAI_API_KEY` | If using `openai` or `openrouter` runner | `openai_api_runner.py`, `openrouter_api_runner.py` |
| `GEMINI_API_KEY` | If using `gemini` runner | `gemini_api_runner.py` |
| `OPENROUTER_API_KEY` | If using `openrouter` runner | `openrouter_api_runner.py` |

> **Note:** In `runner` mode, subscription runtimes such as Claude Code and Codex can drive planner, evaluator, and generator without `ANTHROPIC_API_KEY`.

---

## Runner Installation Requirements

| Runner | Install command | Binary check |
|--------|----------------|-------------|
| `subprocess` | [Download Claude Code](https://claude.ai/download) | `which claude` |
| `sdk` | `pip install -e ".[sdk]"` | `python -c "import claude_code_sdk"` |
| `codex` | `npm install -g @openai/codex` | `which codex` |
| `anthropic` | included in base install | — |
| `openai` | `pip install -e ".[openai]"` | `python -c "import openai"` |
| `gemini` | `pip install -e ".[gemini]"` | `python -c "import google.generativeai"` |
| `openrouter` | `pip install -e ".[openai]"` | `python -c "import openai"` |

---

## Running in Docker

```dockerfile
FROM continuumio/miniconda3:latest

WORKDIR /app
COPY . .

RUN conda create -n harness python=3.12 -y && \
    conda run -n harness pip install -e ".[all-providers]"

ENV ANTHROPIC_API_KEY=""
ENV OPENAI_API_KEY=""
ENV GEMINI_API_KEY=""
ENV OPENROUTER_API_KEY=""

ENTRYPOINT ["conda", "run", "-n", "harness", "harness"]
CMD ["--help"]
```

```bash
docker build -t claude-harness .

docker run \
  -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  -e OPENAI_API_KEY=$OPENAI_API_KEY \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/my_project.yaml:/app/my_project.yaml \
  claude-harness run /app/my_project.yaml --runner openai
```

> **Note:** Agentic runners (`subprocess`, `sdk`, `codex`) require the respective CLI binary inside the container. For Docker deployments, API runners are easier.

---

## Cost Planning by Runner

| Runner | Cost model | Typical cost per feature (5 iterations) |
|--------|-----------|----------------------------------------|
| `subprocess` | Subscription included | ~$0 extra |
| `sdk` | Subscription included | ~$0 extra |
| `codex` | OpenAI subscription included | ~$0 extra |
| `anthropic` | ~$15/$75 per 1M in/out (Opus 4.7) | ~$3–8 |
| `openai` | ~$5/$15 per 1M in/out (GPT-4o) | ~$1–4 |
| `gemini` | ~$1.25/$10 per 1M in/out (2.5 Pro) | ~$0.5–2 |
| `openrouter` | model-dependent | varies |

> In `api` orchestration mode, planner/evaluator bill to `ANTHROPIC_API_KEY`. In `runner` mode, that overhead stays inside the selected subscription coding-agent runtime.

---

## Running as a Background Job

```bash
conda activate harness

nohup harness run my_project.yaml --runner subprocess \
  > harness.log 2>&1 &
echo $! > harness.pid

# Monitor
tail -f harness.log
watch -n 30 "harness status my_project.yaml"

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

> API runners produce text output only — `src/` will not be populated unless you manually apply the generated code.

---

## Upgrading

```bash
conda activate harness
git pull
pip install -e ".[all-providers]"

# Existing features.json files remain compatible unless a Pydantic model field
# was renamed. Check harness/progress/models.py changelog before upgrading
# mid-project.
```
