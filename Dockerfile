FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot scripts
COPY polymarket_scanner.py intelligence.py ./

# Create persistent data directory
# This path will be mounted to a Railway Volume
RUN mkdir -p /data/journal

# Health check: ensure Python works
RUN python3 -c "import intelligence; print('Import OK')"

CMD ["python3", "-u", "polymarket_scanner.py"]
