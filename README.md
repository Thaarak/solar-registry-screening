# Solar Registry Screening

**Turn a solar registry export into a ranked shortlist you can explain.** This Python pipeline validates installation records, narrows the candidate set, and shows exactly how each remaining project scored before you spend time on geospatial lookups or model calls.

## See it work

The bundled example uses five fictional installations. Three pass the qualification filters. The ranking uses 11 rules and a fixed scoring date, so the same inputs produce the same result.

| Rank | Example plant | Capacity | Screening score |
| ---: | --- | ---: | ---: |
| 1 | Example Solar B | 950 kW | 72.9 / 100 |
| 2 | Example Solar A | 6,400 kW | 70.8 / 100 |
| 3 | Example Solar C | 3,200 kW | 66.7 / 100 |

```bash
git clone https://github.com/Thaarak/solar-registry-screening.git
cd solar-registry-screening
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/demo.py --output data/demo_ranking.csv
```

The demo needs no database, API key, or external data after installation. Its CSV includes the individual criterion scores. The [synthetic registry](examples/registry.csv) and [illustrative scorecard](examples/scorecard.json) are included in the repo.

## What I built

| Step | Implementation |
| --- | --- |
| **Ingest and qualify** | Parse PRETOR-style CSV and Excel exports, normalize column names and provinces, and validate records with Pydantic. Apply capacity, vintage, and technology filters. |
| **Rank and explain** | Score qualified records with deterministic Python rules. Return per-criterion points and reasons, with explicit treatment of missing inputs. |
| **Enrich the shortlist** | Match records to regional registries and OpenStreetMap solar geometries, assess coarse grid congestion, and look up cadastral references when coordinates are available. Ambiguous matches are left unresolved. |
| **Review and document** | Load a configurable rubric for optional Claude scoring, track results in PostgreSQL, and generate local Markdown reports. |

The cheap ranking step runs first, to establish a baseline before enrichment. The prototype uses historical screening assumptions and coarse proxies. Treat the output as an example of data-pipeline design and explainable ranking.

## Run the PostgreSQL pipeline

Use a fresh local development database. From the repository root, with the virtual environment active:

```bash
cp .env.example .env
docker compose up -d --wait
python src/db.py
python scripts/load_spain_targets.py examples/registry.csv --skip-cadastral
python scripts/run_stage0_ranking.py --as-of 2026-07-01
python scripts/run_scoring.py --dry-run --ids "$(cat data/stage0_top100_ids.txt)"
```

The first database start loads [schema.sql](schema.sql). The final command checks that the AI scoring candidates and rubric load without sending data anywhere. For an optional live run, set `ANTHROPIC_API_KEY` and pass `--allow-external-scoring` with synthetic or explicitly approved inputs. Set `ANTHROPIC_MODEL` or pass `--model` to select a model, and use `--schema` for your own rubric. The public scoring prompt excludes plant and owner names, internal IDs, precise coordinates, and cadastral references.

Live enrichment scripts access their respective data providers and are separate from the synthetic demo. Review source permissions and attribution requirements before redistributing external data. This repository contains synthetic examples only.

```bash
python -m pytest -q
```
