FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt /tmp/requirements.txt

# Exclude Raspberry-Pi-specific packages for local x86 Docker mock runs.
RUN grep -Ev '^(smbus2|adafruit-blinka|RPi\.GPIO|adafruit-circuitpython-seesaw|adafruit-circuitpython-sht31d|adafruit-circuitpython-tcs34725|hx711)\b' /tmp/requirements.txt > /tmp/requirements.docker.txt \
    && pip install --no-cache-dir -r /tmp/requirements.docker.txt

COPY . /app

CMD ["python", "main.py", "--config", "config.ini"]
