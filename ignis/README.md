# Ignis

[Ignis](https://github.com/Nystik-gh/ignis) runs Obsidian natively in a web
browser by shimming Electron and Node APIs while storing vaults on the server.
On first start, the container downloads the pinned official Obsidian release
into the `obsidian-app` named volume.

Copy `.env.example` to a local `.env`, create the host vault and data
directories with matching `PUID`/`PGID`, and run `docker compose up -d`. Attach
your reverse proxy to the external network named `traefik-net`, or change that
network name for your deployment. Proxy HTTP and WebSockets to `ignis:8080`. No
host port is published.

Ignis has no built-in authentication and requires a secure HTTPS context for
clipboard, graph, and plugin features. Always place HTTPS and authentication
(such as Authelia or reverse-proxy basic auth) in front of it, and set
`WS_ORIGINS` to your public HTTPS origin.

Back up `/vaults` and `/app/data` outside Git. Stop Ignis before copying state
if another sync mechanism writes to the same vault directory.
