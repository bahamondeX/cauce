FROM python:3.13-slim-bookworm
WORKDIR /fixture
RUN pip install --no-cache-dir grpcio==1.85.1
COPY tests/http2/grpc_server.py ./grpc_server.py
CMD ["python", "grpc_server.py"]
