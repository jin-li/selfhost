# Vaultwarden

This Compose stack runs Vaultwarden with SQLite behind an external HTTPS
reverse proxy. It does not publish a host port and joins an existing external
proxy network selected by configuration.

## Configuration

Copy `.env.example` to `.env` and set:

- `VAULTWARDEN_DATA_DIR`: an absolute host path for persistent Vaultwarden
  data;
- `VAULTWARDEN_DOMAIN`: the complete public HTTPS URL;
- `PROXY_NETWORK`: the existing Docker network shared with the reverse proxy;
- `SIGNUPS_ALLOWED`: whether public account creation is enabled;
- `TZ`: the container timezone.

The container runs as numeric UID/GID `1000:100`. Create the data directory
with matching ownership before starting it. If a different identity is needed,
adjust both the Compose `user` value and directory ownership.

The root filesystem is read-only, Linux capabilities are dropped, and `/tmp`
is an ephemeral tmpfs. The admin page is unavailable because this minimal
stack does not configure `ADMIN_TOKEN`.

## Run

Create the configured external proxy network if it does not already exist,
then validate
and start the stack:

```bash
docker network create proxy
docker compose config --quiet
docker compose up -d
docker compose ps
```

Configure the reverse proxy to forward HTTPS traffic to
`http://vaultwarden:8080` on the configured network. Native Bitwarden clients require
direct access to the Vaultwarden API, so an interactive forward-auth layer in
front of the entire service is generally unsuitable.

Disable public signup immediately after creating the intended accounts by
setting `SIGNUPS_ALLOWED=false` and recreating the service.

## Backup

Preserve the entire data directory, including the SQLite database, keys, and
attachments. Use SQLite's online `.backup` command while the service is
running, or stop the service before copying the directory. Test restores using
an isolated data directory.
