FROM python:3.12-slim-bookworm

ENV TZ=Asia/Kolkata \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

# No browser: the bot never logs in to the portal, the Chrome extension does that from your own
# browser. attendance.py still imports Playwright, so the package stays; without a browser to run,
# nothing in this image can log in by accident. tzdata so DAILY_AT means Indian time.
RUN pip install --no-cache-dir playwright==1.63.0 beautifulsoup4 requests \
 && apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY attendance.py bot.py ./
EXPOSE 8080
CMD ["python", "bot.py"]
