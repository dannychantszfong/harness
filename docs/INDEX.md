# Documentation Index — Claude Agent Harness

**Version:** 2.0  
**Last updated:** 2026-05-03  

---

## Diagrams (`docs/diagrams/`)

Open `.drawio` files in [draw.io desktop](https://github.com/jgraph/drawio-desktop) or drag into [diagrams.net](https://app.diagrams.net).

| File | Type | What it shows |
|------|------|---------------|
| [01_architecture_overview.drawio](diagrams/01_architecture_overview.drawio) | Architecture | 6-layer system: CLI → Orchestration → Agents → **Runners** → Persistence → External |
| [02_sequence_agent_interaction.drawio](diagrams/02_sequence_agent_interaction.drawio) | Sequence | Agent interaction across all three phases |
| [03_state_machine_feature.drawio](diagrams/03_state_machine_feature.drawio) | State Machine | Feature lifecycle: PENDING → IN_PROGRESS → PASSING / FAILING |
| [04_flowchart_gan_loop.drawio](diagrams/04_flowchart_gan_loop.drawio) | Flowchart | GAN generator ↔ evaluator loop with context reset branch |
| [05_data_flow.drawio](diagrams/05_data_flow.drawio) | DFD | Data movement between all processes and stores |
| [06_cicd_pipeline.drawio](diagrams/06_cicd_pipeline.drawio) | CI/CD | Code → Lint → Test → Build → Publish stages |

---

## Product (`docs/product/`)

| File | Description |
|------|-------------|
| [PRD.md](product/PRD.md) | Goals, 10 features (incl. pluggable runner system), success metrics |
| [user_stories.md](product/user_stories.md) | 21 user stories across 7 epics (Epic 3 = runner selection, new in v2) |

---

## Technical (`docs/technical/`)

| File | Description |
|------|-------------|
| [architecture.md](technical/architecture.md) | 6-layer breakdown, runner families, agent roles, data model |
| [api_reference.md](technical/api_reference.md) | CLI commands, Python API, YAML config reference, env vars |
| [ADR.md](technical/ADR.md) | 10 Architecture Decision Records (ADR-007–010 cover v2 runner design) |

---

## Operational (`docs/operational/`)

| File | Description |
|------|-------------|
| [runbook.md](operational/runbook.md) | SOP, runner quick reference, 12 incident procedures |
| [deployment_guide.md](operational/deployment_guide.md) | Conda setup, Docker, cost table per runner, background jobs |

---

## Testing (`docs/testing/`)

| File | Description |
|------|-------------|
| [test_plan.md](testing/test_plan.md) | Strategy, 45+ test cases by component (incl. all 7 runners) |
| [test_cases.md](testing/test_cases.md) | Step-by-step specs for runner factory, subprocess, SDK, API, generator integration |

---

## Quick Start

```bash
conda create -n harness python=3.12 -y && conda activate harness
pip install -e ".[all-providers]"
export ANTHROPIC_API_KEY=sk-ant-...
harness runners                              # see all options
harness run examples/web_app.yaml            # prompted to pick runner
harness run examples/web_app.yaml -r sdk     # skip prompt
```

---

## Standard Loop Reference

```
Session Open
  └─ read handoff → git log → features.json → run init.sh → select feature
        │
        ▼
Sprint Contract  (Anthropic API — always)
  └─ generator proposes acceptance criteria + out-of-scope
        │
        ▼
  ┌─ GAN Loop ─────────────────────────────────────────────────────────────┐
  │                                                                         │
  │  Generator: build prompt → delegate to CodeRunner.implement()           │
  │    ├─ Agentic runner (subprocess/sdk/codex):                            │
  │    │    writes files · runs bash · git commit · subscription billing    │
  │    └─ API runner (anthropic/openai/gemini/openrouter):                  │
  │         streams text · token tracking · cost estimate · no file I/O     │
  │                  ↓ self-eval text                                        │
  │  Evaluator: grade (design/orig/craft/fn) → structured score (tool use) │
  │                  ↓                                                       │
  │  score ≥ threshold? → PASSING → next feature                            │
  │  score < threshold  → inject feedback → iterate (up to N times)         │
  │                                                                         │
  └─────────────────────────────────────────────────────────────────────────┘
        │
        ▼
Context Reset (if token budget exceeded)
  └─ write HandoffDocument → reset counter → fresh session with preamble
```

**Key invariant:** Planner and Evaluator always use the Anthropic API regardless of which runner is chosen for the Generator. `ANTHROPIC_API_KEY` is always required.
