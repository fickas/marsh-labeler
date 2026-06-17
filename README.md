# marsh-labeler

A human-in-the-loop labeling app for the crab-burrow active-learning loop. Each
work item is one **superpixel container**; the labeler is asked a near-binary
question driven by the model's **contested pair** ("crab_edge or crab_platform
here?") and answers with a click.

The core idea of the schema: **the question is disposable, the answer is not.**
A container is a per-round question raised by one model generation; a labeler's
answer is a durable fact about a *superpixel* (a place on the ground). So answers
key on the superpixel, not the container, and a retrain can throw away the old
round's containers and ingest new ones without touching a single answer. A
settled superpixel (one with a gold verdict) is never asked again.

This repo is the **spine**: data model + ingestion + a single-page labeler. The
projects/progress pages and React front end are the next phase and hang off
`app/models.py`.

## Layout

```
marsh-labeler/
├── app/
│   ├── config.py        # env-driven settings (local == prod, only vars differ)
│   ├── db.py            # engine + session factory
│   ├── models.py        # SQLAlchemy 2.0 models  <-- the data spine
│   ├── constants.py     # class scheme + queue priority by AREA-metric impact
│   ├── storage.py       # chip storage (local dir | S3/R2)
│   ├── api.py           # labeling API (next / submit / flights)
│   └── main.py          # FastAPI app: API + chips + serves the page
├── web/index.html       # the labeling page (triptych, keyboard-driven)
├── seed.py              # demo flight with placeholder chips (no pipeline needed)
├── ingest/
│   ├── contract.py      # dataclasses: exactly what ingestion reads
│   ├── render.py        # shared chip renderer (containers + exemplars)
│   ├── ingest_flight.py # review queue -> chips + container rows
│   └── ingest_exemplars.py # labeled polygons -> per-class reference chips
├── alembic/             # migrations (0001 = initial schema)
├── flights/example.yaml # one flight's ingestion config
├── docker-compose.yml   # local Postgres + PostGIS
└── requirements.txt
```

## Data model

Hierarchy: **project → flight → rounds of containers**, with a durable layer of
**superpixel verdicts** underneath.

- **projects** — one salt marsh / study site (e.g. *Wellfleet*). A simple project
  is one flight (the 1cm); a complex one is two flights (1cm + 4cm) of the same
  marsh flown about the same day — two labeling tasks under one project.
- **flights** — one drone flight under a project, and the unit a labeler works
  on. Owns its **`class_scheme`** (`{"names": {id: name}, "damage": [ids],
  "ignore_index": int}`) — class count and meanings are per-flight data set at
  ingest, never hardwired, so a project's 1cm and 4cm flights can use different
  models with different schemes. `active_round` says which round's questions the
  queue is currently serving.
- **containers** — one superpixel review item *for one round*: geometry, abstain
  stats, the contested pair (`class_a`/`class_b`), `model_probs`, `chip_keys`,
  serving `priority`. **Ephemeral** — keyed by `(flight_id, round,
  superpixel_id)` and regenerated every retrain. Nothing durable hangs off a
  container.
- **users** — labelers, with a `role` (labeler / admin / ecologist).
- **labels** — one labeler's answer about a *superpixel*, keyed
  `(flight_id, superpixel_id, user_id)` — **not** to a container. Stamped with the
  `round` and contested pair it was answered under, so the audit trail stays
  readable after that round's containers are gone. Multiple users answering the
  same superpixel is how inter-annotator agreement works.
- **resolved_labels** — the gold **verdict per `(flight_id, superpixel_id)`**
  (single / majority / adjudicated). **This is the durable training fact**: it
  rasterizes back onto the superpixel raster to build the mask, and once set it
  retires that superpixel from all future rounds. A *skip* never produces a
  verdict, so genuinely-skipped superpixels can resurface in a later round.
- **exemplars** — reference chips per class, rendered from your labeled polygons
  with the same views as the containers. The UI shows these as a calibration
  gallery (surface the contested pair's two classes first). Built separately by
  `ingest_exemplars.py`, so you can re-curate examples without re-ingesting.

**Rounds / the AL loop.** Each retrain is a round. Ingest a round (`round` in the
config, or `--round N`) and it replaces just that round's containers, points the
flight's queue at it, and skips any superpixel that already has a verdict. The
queue serves `active_round` only, skipping superpixels you've answered and
superpixels that are settled, and honors each container's `replication_target`
(single-coverage drops out once answered; replicated stays open until it hits the
target). At `replication_target = 1`, one label settles the superpixel.

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

# click through the UI now, with placeholder chips and no pipeline data:
python seed.py                              # demo flight; --wipe to reset
# then open http://localhost:8000

# or ingest a real flight (edit flights/example.yaml to point at your outputs)
python -m ingest.ingest_flight --config flights/example.yaml

# build the per-class reference gallery from your labeled polygons
python -m ingest.ingest_exemplars --config flights/example.yaml
```

Open `http://localhost:8000`, enter an email, and you get the labeling triptych: the container chip between the two contested classes' exemplars, `1`/`2` to pick a side, the full palette for a different class, and skip / none / split escapes.

Re-run / advance a round (replaces just that round's containers; answers and
verdicts, keyed to the superpixel, are untouched):

```bash
python -m ingest.ingest_flight --config flights/example.yaml --round 2
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
