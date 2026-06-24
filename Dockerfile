FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agentego/ ./agentego/

RUN mkdir -p /data && chown 1000:1000 /data

EXPOSE 8765

CMD ["uvicorn", "agentego.main:app", "--host", "0.0.0.0", "--port", "8765", "--log-level", "info"]
