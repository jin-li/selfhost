# Feishin

Feishin is a browser-based music player for Navidrome and other compatible
music servers. This stack serves only the web application; the browser talks
directly to the configured server URL.

Copy `.env.example` to `.env`, set the public music-server URL, create the
external `traefik-net` network, and start the service:

```sh
docker network create traefik-net
docker compose up -d
```

The container listens on port 9180 inside the proxy network and does not
publish a host port. `SERVER_LOCK=true` keeps the configured server fixed while
still allowing each user to enter their own account credentials. Analytics are
disabled.
