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

# s6 service: holds Plex until claim token is ready
COPY s6/plex-setup/run /etc/cont-init.d/50-plex-claim-wait
RUN chmod +x /etc/cont-init.d/50-plex-claim-wait

# s6 service: the setup sidecar web server
COPY s6/setup-server/run /etc/services.d/setup-server/run
RUN chmod +x /etc/services.d/setup-server/run

EXPOSE 32400 7070

ENV PLEX_SETUP_PORT=7070
