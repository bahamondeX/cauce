FROM python:3.12-slim-bookworm@sha256:44cf1793efa7c7a476bb0f2cff81443f4a072c90c1cb127e2924933daa3e8c64
RUN apt-get update && apt-get install -y --no-install-recommends \
    uwsgi=2.0.21-5.1 uwsgi-plugin-python3=2.0.21-5.1 \
    && rm -rf /var/lib/apt/lists/*
COPY wsgi_fixture.py /fixture/wsgi_fixture.py
WORKDIR /fixture
