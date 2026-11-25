FROM python:3.13-slim-bookworm
WORKDIR /fixture
RUN pip install --no-cache-dir h2==4.4.1
COPY tests/http2/early_backend.py ./early_backend.py
CMD ["python", "early_backend.py"]
