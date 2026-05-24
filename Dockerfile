FROM plexinc/pms-docker:plexpass

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip python3-venv && \
    python3 -m venv /opt/setup-venv && \
    /opt/setup-venv/bin/pip install --no-cache-dir flask requests pyyaml && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY hexos_hooks.py /app/hexos_hooks.py
COPY plex_hook.py /app/plex_hook.py
COPY setup_server.py /app/setup_server.py
COPY templates/ /app/templates/

# Runs before 40-plex-first-run — serves setup UI, gets claim token, injects PLEX_CLAIM
COPY s6/cont-init/39-plex-setup /etc/cont-init.d/39-plex-setup
RUN chmod +x /etc/cont-init.d/39-plex-setup

# Override plex service run script (stock passthrough — setup is done by init time)
COPY s6/plex/run /etc/services.d/plex/run
RUN chmod +x /etc/services.d/plex/run

# Post-install hook service — sets preferences and creates libraries after Plex starts
COPY s6/plex-hook/run /etc/services.d/plex-hook/run
COPY s6/plex-hook/finish /etc/services.d/plex-hook/finish
RUN chmod +x /etc/services.d/plex-hook/run /etc/services.d/plex-hook/finish

EXPOSE 32400
