FROM python:3.13-slim-bookworm
RUN pip install --no-cache-dir uvicorn==0.38.0
COPY examples/api.py /app/api.py
WORKDIR /app
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
