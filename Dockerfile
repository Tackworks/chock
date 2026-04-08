FROM python:3.12-slim

WORKDIR /app
COPY server.py .
COPY static/ static/

RUN pip install --no-cache-dir fastapi uvicorn

EXPOSE 8796

ENV CHOCK_HOST=0.0.0.0
ENV CHOCK_PORT=8796
ENV CHOCK_DB=/data/approvals.db

VOLUME /data

CMD ["python", "server.py"]
