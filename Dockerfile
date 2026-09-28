FROM python:3.12-slim-bookworm

ENV TZ=Asia/Kolkata \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    BROWSER_CHANNEL=chromium \
    DATA_DIR=/data

# Chromium + its system libraries, and tzdata so DAILY_AT means Indian time
RUN pip install --no-cache-dir playwright==1.63.0 beautifulsoup4 requests \
 && playwright install --with-deps --no-shell chromium \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY attendance.py bot.py ./
CMD ["python", "bot.py"]
