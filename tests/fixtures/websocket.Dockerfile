FROM python:3.13-slim-bookworm
RUN pip install --no-cache-dir websockets==15.0.1
COPY tests/fixtures/websocket_server.py /app/server.py
WORKDIR /app
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/server.py"]
