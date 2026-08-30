FROM python:3.12-slim

# Cache-buster: forces Railway to rebuild instead of reusing a stale image
ARG CACHEBUST=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

ENV PORT=8000
EXPOSE 8000

# TEMPORARY DIAGNOSTIC: idle instead of crash-looping, so we can open a
# Railway Console shell into a live container and inspect it directly.
# Revert this once we've confirmed the real start command works.
CMD ["sleep", "infinity"]
