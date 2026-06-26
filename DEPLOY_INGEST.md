# Production Ingest Guide (Railway)

How to load a real flight into the **deployed** Marsh Labeler on Railway, so the
team can label it at the public URL. This is the production counterpart to
`RUNBOOK.md` (which covers the *local* loop). Read both: the bring-in/export GT
mechanics live in `RUNBOOK.md`; this file covers the Railway/bucket specifics.

## The shape of it

- **App + database live on Railway.** The web service (`marsh-labeler`) and a
  PostGIS database (`postgis`) are already deployed. You don't redeploy them to
  ingest -- they just sit there serving.
- **Ingest runs on your laptop**, pointed at the Railway database (via its public
  TCP proxy) and the chip bucket. Your laptop has the rasters and the geo stack;
  Railway never sees them.
- **Chips go to the bucket** (`chips` service, S3-compatible). The deployed app
  proxies reads from it (`/chips/...`). The bucket is private; only the app reads it.
- **Verdicts live in the Railway database.** Labelers write to it through the app.

So a production ingest = run ingest locally with a Railway-pointed env, then the
flight appears at the URL.

## One-time things that are already standing

You set these up once; they persist:

- `postgis` service (PostGIS image + volume at `/var/lib/postgresql/data`,
  `PGDATA=/var/lib/postgresql/data/pgdata`).
- `marsh-labeler` web service (Dockerfile build) with env: `DATABASE_URL` (-> postgis),
  `STORAGE_BACKEND=s3`, the five `STORAGE_S3_*` bucket creds, `BASIC_AUTH_PASS`.
- `chips` bucket.
- Schema migrations run automatically on each web deploy (`start.sh` -> `alembic upgrade head`).

If you ever recreate the database, the only gotcha is the **password**: the
PostGIS image only sets it on first init of an empty volume. If they ever drift,
fix from the postgis Console with
`psql -U marsh -d marsh -c "ALTER USER marsh WITH PASSWORD '...';"` and set
`POSTGRES_PASSWORD` to match.

## Step 1 -- get the current database public endpoint

The web service reaches the DB privately (`postgis.railway.internal`), but your
**laptop** needs the public TCP proxy. Find the current host:port:

- Railway -> `postgis` -> Settings -> Networking -> the TCP Proxy row, OR
- `railway variables -s postgis | grep -i tcp` (RAILWAY_TCP_PROXY_DOMAIN / _PORT)

It looks like `reseau.proxy.rlwy.net:50921`. **This can change** if the proxy is
deleted/recreated, and a proxy sometimes only activates after you **redeploy**
`postgis`. Grab the current value each time.

## Step 2 -- the laptop env file (`.env.railway`)

Create `.env.railway` in the repo root. **It holds secrets -- it is gitignored
(`.env.*`); never commit it.** Single-quote every value (the bucket secret
contains characters the shell will choke on otherwise):

```sh
DATABASE_URL='postgresql://marsh:<DB_PASSWORD>@<PROXY_HOST>:<PROXY_PORT>/marsh'
STORAGE_BACKEND='s3'
STORAGE_S3_ENDPOINT='https://t3.storageapi.dev'
STORAGE_S3_BUCKET='<bucket-name>'
STORAGE_S3_ACCESS_KEY='<access-key-id>'
STORAGE_S3_SECRET_KEY='<secret-access-key>'
STORAGE_S3_REGION='auto'
CHIP_BASE_URL='/chips'
```

`CHIP_BASE_URL=/chips` is essential: it makes the chip URLs written into the DB
point at the app's proxy route (not at the bucket directly, which is private).

Load it and **verify the secrets actually loaded** before ingesting:

```sh
set -a; source .env.railway; set +a
echo "secret length: ${#STORAGE_S3_SECRET_KEY}"   # must be non-zero
python3 -c "import psycopg,os; psycopg.connect(os.environ['DATABASE_URL'].replace('+psycopg','')); print('DB OK')"
```

## Step 3 -- bring the flight's files in from Drive

Same as `RUNBOOK.md` section 1: copy the pipeline outputs into `data/<flight>/`
with the canonical names (abstain_review.gpkg, pansharp_5band.tif, superpixels.tif,
abstain.tif [the **reduced** one], softmax.tif, pan.tif, selection_params.json,
gt_labeled.gpkg), and point the flight YAML at them.

## Step 4 -- preflight, then ingest into Railway

```sh
set -a; source .env.railway; set +a          # if not already loaded this shell

python -m ingest.preflight        --config flights/<flight>.yaml
python -m ingest.ingest_flight    --config flights/<flight>.yaml
python -m ingest.ingest_exemplars --config flights/<flight>.yaml --replace
```

`ingest_flight` renders each container's chips, uploads them to the bucket, and
writes the container rows (with `/chips/...` URLs) into the Railway database.
Success looks like: `Ingested N containers ...` and `ingested M exemplars ...`.

## Step 5 -- verify at the URL

Open the app, log in (any username + the shared password), pick the flight under
Tasks. Confirm tasks appear **and chips render**. If tasks show but chips are
blank, the web service can't read the bucket -- check that `STORAGE_S3_SECRET_KEY`
on the `marsh-labeler` service matches the real secret, and redeploy.

## Getting results back from Railway

The labeling happens on Railway, so the verdicts live in the Railway database.
There are two things you might want back, for two different purposes.

### A. Verdicts as ground-truth polygons (the usual goal -- feeds training)

`export_gt`, run from your laptop with the Railway env loaded. It reads the
settled verdicts out of the Railway DB and reconciles them into your GT file.
Bring the canonical `gt_labeled.gpkg` in from Drive first (see RUNBOOK's "never
export onto a missing GT" rule and the `--init` guard):

```sh
set -a; source .env.railway; set +a
python export_gt.py --flight <flight>
```

Because `DATABASE_URL` points at Railway, this connects to the Railway DB, pulls
the verdicts, vectorizes them against your local `superpixels.tif`, reconciles
into `gt_labeled.gpkg`, and you sync that back to Drive. Same command as the local
loop -- only the env differs.

### B. The raw records (audit / analysis / backup)

A quick look -- query Railway directly:

```sh
set -a; source .env.railway; set +a
DBURL=$(python3 -c "import os;print(os.environ['DATABASE_URL'].replace('+psycopg',''))")
psql "$DBURL" -c "select action, count(*) from labels group by action;"
psql "$DBURL" -c "select class_id, out_of_scope, method, count(*) from resolved_labels group by 1,2,3;"
```

A full backup -- dump the entire Railway database to a file on your laptop:

```sh
set -a; source .env.railway; set +a
DBURL=$(python3 -c "import os;print(os.environ['DATABASE_URL'].replace('+psycopg',''))")
pg_dump "$DBURL" -Fc -f railway_marsh_$(date +%Y%m%d).dump
```

That `.dump` is a complete snapshot (labels, verdicts, containers, exemplars).
Keep it as a backup, or restore into a local Postgres for offline analysis:
`pg_restore -d <local-db> railway_marsh_YYYYMMDD.dump`. Worth doing periodically
once the team is labeling real marshes, so the verdicts don't live only on Railway.

**Which to use:** `export_gt` for the *product* (GT polygons for the next training
run); `pg_dump` for the *records* (safety + audit trail). Both connect through the
TCP proxy, so the proxy must be up and `.env.railway`'s host:port current.

## Scale note for real marshes

A real marsh (~8K tiles) is far larger than the synthetic demo (~11 containers).
Expect:

- **Ingest takes a while** -- it renders + uploads ~3 chips per container (tens of
  thousands of small PNGs). Let it run; it's network-bound on the uploads.
- Only superpixels that pass the abstain rule become containers, so the labelable
  count is the abstain count, not all 8K tiles.
- The DB rows stay small; the chips are the bulk, and they live in the bucket.

## Troubleshooting (the things that bit us)

- **`missing: aws_secret_access_key`** -> the secret didn't load. Re-`source` the
  env in *this* shell; confirm `echo ${#STORAGE_S3_SECRET_KEY}` is non-zero;
  single-quote the value; keep it on one line.
- **`could not translate host name "postgis.railway.internal"`** -> you used the
  internal host from the laptop. Use the public proxy host:port instead.
- **`password authentication failed for user "marsh"`** -> the URL's password
  differs from the DB's. Test the *network* path:
  `PGPASSWORD=... psql -h <proxy-host> -p <proxy-port> -U marsh -d marsh -c "select 1;"`.
  Local `psql -U marsh` inside the container uses trust auth and hides mismatches.
- **Deploy logs look like an old failure** -> Railway shows historical attempts;
  confirm you're viewing the newest deployment, or test live from the container
  Console (`python -c "import psycopg,os; psycopg.connect(...)"`).
- **TCP proxy shows only `:5432`, no host** -> redeploy `postgis`; the proxy
  activates on deploy.
