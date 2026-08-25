FROM python:3.12-slim

RUN pip install --no-cache-dir "anyio>=4.7" && \
    mkdir -p /opt/preflightkit/engine /opt/preflightkit/traffic

COPY src/preflightkit/__init__.py /opt/preflightkit/
COPY src/preflightkit/engine/__init__.py /opt/preflightkit/engine/
COPY src/preflightkit/engine/bus.py /opt/preflightkit/engine/
COPY src/preflightkit/engine/events.py /opt/preflightkit/engine/
COPY src/preflightkit/traffic/__init__.py /opt/preflightkit/traffic/
COPY src/preflightkit/traffic/accept_probe.py /opt/preflightkit/traffic/
COPY src/preflightkit/traffic/client.py /opt/preflightkit/traffic/
COPY src/preflightkit/traffic/generator.py /opt/preflightkit/traffic/
COPY spikes/sidecar-probe/probe.py /opt/sidecar-probe.py

ENV PYTHONPATH=/opt
ENTRYPOINT ["python", "/opt/sidecar-probe.py"]
