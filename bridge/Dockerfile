FROM python:3.12-slim

LABEL org.opencontainers.image.title="nominal-bridge" \
      org.opencontainers.image.description="Config-driven bridge that streams mission-system and sensor data into Nominal"

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BRIDGE_CONFIG=/config/bridge.yaml

WORKDIR /src
COPY pyproject.toml ./
COPY nominal_bridge ./nominal_bridge
# install, then generate the Couloir gRPC stubs into the installed package
RUN pip install --no-cache-dir . \
 && cd / && python -c "from nominal_bridge.sources.squall import load_proto; load_proto()" \
 && rm -rf /src

# /config/bridge.yaml  the bridge config (mount yours here)
# /plugins             optional *.py plugins, imported at start-up
# /capture             raw recordings; mount a volume to keep them
RUN mkdir -p /config /plugins /capture
VOLUME ["/capture"]
WORKDIR /

ENTRYPOINT ["nominal-bridge"]
CMD ["run"]
