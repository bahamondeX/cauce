FROM python:3.13-slim-bookworm
WORKDIR /test
RUN pip install --no-cache-dir h2==4.4.1
COPY tests/http2/headers_timeout.py ./headers_timeout.py
CMD ["python", "headers_timeout.py"]
