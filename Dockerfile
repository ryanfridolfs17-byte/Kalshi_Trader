FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

# Default state directory — on Railway, mount a Volume at /data
ENV STATE_DIR=/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create state directory (Railway Volume will overlay this if mounted)
RUN mkdir -p /data

COPY . .

CMD ["python", "kalshi_bot.py"]
