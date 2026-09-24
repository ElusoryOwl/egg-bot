FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persist the SQLite DB outside the container's writable layer so it
# survives `docker compose up --force-recreate` / image rebuilds.
ENV DB_PATH=/data/egg_bot.db
RUN mkdir -p /data

# Run as a non-root user rather than the container default of root.
RUN useradd --create-home --uid 1000 eggbot && chown -R eggbot:eggbot /app /data
USER eggbot

VOLUME ["/data"]

CMD ["python", "bot.py"]