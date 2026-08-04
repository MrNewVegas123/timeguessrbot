FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY timeguessr_bot.py .

# SQLite DB lives here — mount a persistent volume at this path on
# Railway/Fly.io so data survives redeploys.
ENV TIMEGUESSR_DB=/data/timeguessr.db
RUN mkdir -p /data

CMD ["python", "timeguessr_bot.py"]
