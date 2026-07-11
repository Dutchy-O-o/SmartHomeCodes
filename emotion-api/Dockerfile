FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY detector.py .
COPY model.py .
COPY main.py .
COPY pi_config.py .
COPY logging_config.py .
COPY telemetry.py .
COPY relay_subscriber.py .
COPY cert.pem .
COPY key.pem .
COPY models/ models/

RUN mkdir -p /var/log/smarthome

# Expected mounts at run time:
#   -v /etc/smarthome:/etc/smarthome:ro                              (pi.toml)
#   -v /home/ramazan/Desktop/python/certs:/app/certs:ro              (AWS IoT thing cert)
#   -v /var/log/smarthome:/var/log/smarthome                         (JSON-line logs)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--ssl-keyfile", "key.pem", "--ssl-certfile", "cert.pem"]
