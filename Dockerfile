FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot scripts and templates
COPY polymarket_scanner.py intelligence.py news_intel.py ./
COPY templates/ ./templates/

# Create persistent data directory
# This path will be mounted to a Railway Volume
RUN mkdir -p /data/journal

# Startup
CMD ["python3", "-u", "polymarket_scanner.py"]
