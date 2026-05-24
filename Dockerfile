FROM plexinc/pms-docker:plexpass

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip python3-venv && \
    python3 -m venv /opt/setup-venv && \
    /opt/setup-venv/bin/pip install --no-cache-dir flask requests && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY hexos_hooks.py /app/hexos_hooks.py
COPY plex_hook.py /app/plex_hook.py
COPY setup_server.py /app/setup_server.py
COPY templates/ /app/templates/

# Replace the Plex service run script with our setup-then-start wrapper
COPY s6/plex/run /etc/services.d/plex/run
RUN chmod +x /etc/services.d/plex/run

EXPOSE 32400
