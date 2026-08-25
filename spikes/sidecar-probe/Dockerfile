FROM python:3.12-slim

RUN pip install --no-cache-dir "anyio>=4.7"

COPY src/preflightkit /opt/preflightkit
COPY spikes/sidecar-probe/probe.py /opt/sidecar-probe.py

ENV PYTHONPATH=/opt
ENTRYPOINT ["python", "/opt/sidecar-probe.py"]
