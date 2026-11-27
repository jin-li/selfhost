# NetBird

This Docker Compose stack runs the combined NetBird server and dashboard with
an embedded identity provider and SQLite. The server and Traefik must share
the external `traefik-net` Docker network.

1. Copy `config.example.yaml` to `config.yaml` and `dashboard.env.example` to
   `dashboard.env`. Replace `netbird.example.com` in both files with your
   public hostname.
2. For a new installation, generate the relay secret with
   `openssl rand -hex 32` and the store encryption key with
   `openssl rand -base64 32`. Set them in `config.yaml`. Keep both values
   when redeploying an existing instance.
3. Set the trusted proxy ranges in `config.yaml` to the Traefik address or
   its Docker network subnet.
4. Copy the Traefik example below into the active dynamic configuration
   directory, replace the example hostname, and configure TLS for that host.
   Make UDP port 3478 reachable by clients for STUN.
5. Run `docker compose config --quiet` and `docker compose up -d`, then open
   the dashboard to finish setup.

Stop the server before backing up `data/`. Include `config.yaml` and
`dashboard.env` in the private backup. The server reuses cached GeoLite
databases on restart. A fresh data directory requires an initial download.

[Traefik routing example](../traefik/dynamic.example/netbird.yml)
