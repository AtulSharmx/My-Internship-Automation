# Wellfound bot on an always-on Linux server.
#
# Headful Chromium runs inside Xvfb, so the browser keeps the same realistic
# fingerprint it has on a desktop. The base image ships the exact Playwright
# version and all Chromium system dependencies.
#
# If this tag is ever missing, use :v1.64.0-jammy instead.
FROM mcr.microsoft.com/playwright/python:v1.64.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata \
    WF_HEADLESS=false \
    DISPLAY=:99

# tzdata = correct local "today" for /report and the 20:00 daily schedule.
# x11vnc = optional one-time interactive Wellfound login (see deploy/README.md).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata x11vnc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Playwright is already installed in the base image; only add the other deps.
RUN pip install --no-cache-dir "httpx>=0.27" "python-dotenv>=1.0"

COPY . .

# data/, logs/ and browser_profile/ are mounted from the host by docker-compose.
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1440x900x24", "python", "run.py"]
