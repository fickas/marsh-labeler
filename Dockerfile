# Runtime image for the Marsh Labeler web service.
#
# Deliberately slim: the running app needs only FastAPI + SQLAlchemy + psycopg +
# GeoAlchemy2 (all pure-Python or wheels), so there is NO GDAL/rasterio/geopandas
# here. The heavy geo + ingest stack lives on your laptop, which is where ingest
# runs. This keeps Railway builds fast and reliable.
FROM python:3.12-slim

WORKDIR /srv

# Only the runtime deps -- see requirements-app.txt for why this is a short list.
COPY requirements-app.txt .
RUN pip install --no-cache-dir -r requirements-app.txt

# App code, static pages, and the migration tree (start.sh runs alembic on boot).
COPY app ./app
COPY web ./web
COPY alembic ./alembic
COPY alembic.ini .
COPY start.sh .
RUN chmod +x start.sh

# Railway provides $PORT at runtime; this default is for local `docker run`.
ENV PORT=8000
EXPOSE 8000

CMD ["./start.sh"]
