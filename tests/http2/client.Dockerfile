FROM python:3.13-slim-bookworm
WORKDIR /test
RUN pip install --no-cache-dir grpcio==1.85.1 h2==4.4.1
COPY tests/http2/client.py ./client.py
COPY tests/http2/lifecycle.py ./lifecycle.py
CMD ["python", "client.py"]
