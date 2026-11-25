FROM python:3.13.2-slim-bookworm@sha256:6b3223eb4d93718828223966ad316909c39813dee3ee9395204940500792b740
COPY constraints.txt requirements.txt /lab/
RUN python -m pip install --no-cache-dir --disable-pip-version-check -c /lab/constraints.txt -r /lab/requirements.txt
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AWS_EC2_METADATA_DISABLED=true
ENV AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null
ENV PYTHONPATH=/reference:/reference/deploy
USER 10001:10001
ENTRYPOINT ["python", "/lab/server.py"]
