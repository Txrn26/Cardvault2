# CardVault, containerized for TrueNAS (or anywhere else running Docker).
#
# No browser in this image at all -- pricing comes from eBay's official
# Browse API (see backend/ebay_api.py), not from scraping a page and
# fighting a bot-detector to get past a CAPTCHA. That's what makes this
# image small and boring: just Python + a couple of pure-HTTP dependencies.

FROM python:3.12-slim

# Standard practice for containerized Python apps -- ensures nothing gets
# held back in a stdout buffer, so `docker logs` shows output as it happens.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

WORKDIR /app/backend
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
