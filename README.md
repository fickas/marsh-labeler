# marsh-labeler

A human-in-the-loop labeling app for the crab-burrow active-learning loop. Each
work item is one **superpixel container**; the labeler is asked a near-binary
question driven by the model's **contested pair** ("crab_edge or crab_platform
here?") and answers with a click. Answers resolve to a gold label per container
that rasterizes back into the training mask for the next retrain.

This repo is the **spine**: data model + ingestion. The labeling API and React
front end are the next phase and hang off `app/models.py`.

## Layout

```
marsh-labeler/
├── app/
│   ├── config.py        # env-driven settings (local == prod, only vars differ)
│   ├── db.py            # engine + session factory
│   ├── models.py        # SQLAlchemy 2.0 models  <-- the data spine
│   ├── constants.py     # class scheme + queue priority by AREA-metric impact
│   ├── storage.py       # chip storage (local dir | S3/R2)
│   └── main.py          # FastAPI stub (health only for now)
├── ingest/
│   ├── contract.py      # dataclasses: exactly what ingestion reads
│   └── ingest_flight.py # pipeline outputs -> chips + Postgres rows
├── alembic/             # migrations (0001 = initial schema)
├── flights/example.yaml # one flight's ingestion config
├── docker-compose.yml   # local Postgres + PostGIS
└── requirements.txt
```

## Data model

- **flights** — one drone flight / dataset, including its **`class_scheme`**
  (`{"names": {id: name}, "damage": [ids], "ignore_index": int}`). The class
  count and meanings are per-flight data, set at ingest from the flight config —
  no class id or count is hardwired in code, so the scheme can change on real
  data without edits. `pair_priority` takes the damage set as an argument.
- **containers** — one superpixel review item: geometry, abstain stats, the
  contested pair (`class_a`/`class_b`), `model_probs`, `chip_keys`, and a
  serving `priority`. Keyed by `(flight_id, superpixel_id)`.
- **users** — labelers, with a `role` (labeler / admin / ecologist).
- **labels** — raw answer per `(container, user)`. Multiple users can answer the
  same container, which is how inter-annotator agreement works. This is the
  audit trail.
- **resolved_labels** — the single gold label per container (single / majority /
  adjudicated). **This is what rasterizes back to the training mask** — via
  `superpixel_id`, not geometry.

`priority` is set at ingest by `app.constants.pair_priority`: ties that cross
the damage boundary (exactly one of the two classes is a crab class) are served
first, because those are the ones that move the crab-damage AREA metric.

## Local development

```bash
docker compose up -d                       # Postgres + PostGIS on :5432
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                        # defaults already point at the docker db
alembic upgrade head                        # create the schema
uvicorn app.main:app --reload               # http://localhost:8000/health

# ingest a flight (edit flights/example.yaml to point at your pipeline outputs)
python -m ingest.ingest_flight --config flights/example.yaml
```

Re-ingest a flight (clears its containers; labels cascade):

```bash
python -m ingest.ingest_flight --config flights/example.yaml --replace
```

## Integration seam

`ingest_flight` expects the review GeoPackage from
`build_abstain_review_polygons()` to carry, besides `geometry`:
`superpixel_id, n_pixels, abstain_frac, pair_purity, diffuse_frac, pair_code,
class_a, class_b, is_diffuse` (see `ingest/contract.REQUIRED_REVIEW_COLUMNS`).

Two of these are a small addition to the current abstain_review output: emit the
**`superpixel_id`** of each container, and split the decoded contested pair into
**`class_a`/`class_b`** (lower id first). Everything else it already produces.

## Deploying to Railway (later)

1. Push this repo to GitHub.
2. On Railway: add a **PostgreSQL** service, then a service from the GitHub repo.
3. Set env vars on the app service (`DATABASE_URL` from the PG service;
   `STORAGE_BACKEND=s3` plus the R2/S3 vars).
4. Run `alembic upgrade head` (release command), then the ingest job against the
   Railway Postgres.

The app code is identical local vs. Railway; only the env vars change.

## Next phase

- API: `next-container` (priority-ordered, skipping what I've already labeled),
  `submit-label`, `resolve`, `agreement`.
- React front end: chip + contested-pair buttons + band/CIR/geomorphic toggles +
  `model_probs` display + keyboard shortcuts.
- Export: resolved labels -> class raster (255 elsewhere) -> retrain.
