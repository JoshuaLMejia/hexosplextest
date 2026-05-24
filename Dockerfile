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

# s6 service: waits for claim token then signals Plex to start
COPY s6/plex-setup/run /etc/services.d/plex-setup/run
COPY s6/plex-setup/finish /etc/services.d/plex-setup/finish
RUN chmod +x /etc/services.d/plex-setup/run /etc/services.d/plex-setup/finish

# s6 service: the setup sidecar web server
COPY s6/setup-server/run /etc/services.d/setup-server/run
RUN chmod +x /etc/services.d/setup-server/run

# Override the Plex service to wait for setup-done signal
COPY s6/plex/run /etc/services.d/plex/run
RUN chmod +x /etc/services.d/plex/run

EXPOSE 32400 7070

ENV PLEX_SETUP_PORT=7070
