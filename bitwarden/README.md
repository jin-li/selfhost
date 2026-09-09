# Bitwarden Lite

This stack runs the official single-container Bitwarden Lite deployment with
SQLite. It expects HTTPS termination at an external reverse proxy and joins the
external `traefik-net` network without publishing a host port.

## Configuration

The service manifest supplies these values:

- `BITWARDEN_DATA_DIR`: host directory for persistent configuration, SQLite,
  attachments, identity material, and logs;
- `BW_DOMAIN`: public hostname without a URL scheme;
- `BW_INSTALLATION_ID` and `BW_INSTALLATION_KEY`: secret credentials obtained
  from <https://bitwarden.com/host/>;
- `DISABLE_USER_REGISTRATION`: set to `false` only while creating an intended
  account, then change it to `true` and recreate the container.

The bind directories must be writable by container UID/GID `999:999`. The
container otherwise uses a read-only root filesystem, drops all capabilities,
and enables `no-new-privileges`.

Use a dedicated HTTPS hostname. Do not place an interactive forward-auth layer
in front of the route because native clients need direct access to Bitwarden's
vault, API, identity, and notification endpoints.

## Connect clients

Install the official Bitwarden application or browser extension. Before login,
select **Self-hosted** and enter:

```text
https://vault.example.com
```

Replace the example with the deployed hostname and sign in with an existing
account. On desktop browsers, make the Bitwarden extension the default password
manager and disable the browser's competing password manager. The extension
will then offer to save and fill both passwords and passkeys.

On Android, enable Bitwarden under **Settings → Autofill** and select it as the
system password/passkey provider. On iOS and iPadOS, enable
**Settings → General → AutoFill & Passwords → AutoFill Passwords and Passkeys**
and select Bitwarden under **AutoFill From**.

Configure the CLI with:

```bash
bw logout
bw config server https://vault.example.com
bw login
```

Passkeys saved for other sites are separate from passkeys used to log in to or
unlock Bitwarden. Bitwarden login/unlock depends on browser and authenticator
PRF support, so retain the master password and offline recovery material.

## Validation and backup

Validate the composed stack with its rendered environment and check the live
endpoint from inside the container:

```bash
docker compose --project-name bitwarden \
  --env-file /run/container-config/HOST/bitwarden/compose.env \
  -f docker-compose.yml config --quiet
docker exec bitwarden curl --silent --fail http://localhost:8080/alive
```

Before an image update, record the current image and make a consistent SQLite
