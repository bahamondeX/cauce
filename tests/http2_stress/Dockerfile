FROM python:3.13-slim-bookworm
WORKDIR /test
RUN pip install --no-cache-dir h2==4.4.1
COPY tests/http2_stress/runner.py /test/runner.py
CMD ["python", "/test/runner.py"]
