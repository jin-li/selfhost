# Self-hosted Docker apps

Portable Docker Compose stacks, example configuration, and optional service
manifests. Each stack has its own prerequisites; reverse-proxy stacks can use
Traefik and Cloudflare Tunnel, while local inference stacks need the appropriate
GPU runtime. An external deployment renderer is not required for Compose usage.

## Table of contents

- [Stack index](#stack-index)
- [Manual deployment](#manual-deployment)
- [Service manifests](#service-manifests)
- [Contributing](#contributing)

## Stack index

Entries link to a service guide when one exists, or to its stack directory.
Available stacks are not a list of services deployed by any particular operator.

| Stack / guide | Purpose | Definition |
| --- | --- | --- |
| [audiobook](audiobook/README.md) | EPUB conversion and a resumable web queue | [Compose](audiobook/docker-compose.yml) |
| [audiobookshelf](audiobookshelf/README.md) | Audiobook and podcast library server | [Compose](audiobookshelf/docker-compose.yml) |
| [authelia](authelia/) | Authentication gateway | [Compose](authelia/docker-compose.yml) |
| [calibre](calibre/) | Calibre-Web ebook library | [Compose](calibre/docker-compose.yml) |
| [chevereto](chevereto/) | Image hosting | [Compose](chevereto/docker-compose.yml) |
| [cloudflared](cloudflared/README.md) | Cloudflare Tunnel connector | [Compose](cloudflared/docker-compose.yml) |
| [ddns](ddns/) | Dynamic DNS updater | [Compose](ddns/docker-compose.yml) |
| [feishin](feishin/README.md) | Browser music client | [Compose](feishin/docker-compose.yml) |
| [frankmd](frankmd/) | Markdown editor | [Compose](frankmd/docker-compose.yml) |
| [gitlab](gitlab/) | GitLab development platform | [Compose](gitlab/docker-compose.yml) |
| [headscale](headscale/README.md) | Tailscale-compatible control server and Headplane | [Compose](headscale/docker-compose.yml) |
| [immich](immich/) | Photo and video library | [Compose](immich/docker-compose.yml) |
| [jellyfin](jellyfin/) | Media library and streaming | [Compose](jellyfin/docker-compose.yml) |
| [label-studio](label-studio/) | Data labeling with PostgreSQL | [Compose](label-studio/docker-compose.yml) |
| [navidrome](navidrome/) | Music library server | [Compose](navidrome/docker-compose.yml) |
| [nextcloud](nextcloud/) | File sync and collaboration | [Compose](nextcloud/docker-compose.yml) |
| [ollama](ollama/) | Local model inference | [Compose](ollama/docker-compose.yml) |
| [open-webui](open-webui/) | Browser interface for AI backends | [Compose](open-webui/docker-compose.yml) |
| [picard](picard/README.md) | MusicBrainz Picard browser desktop | [Compose](picard/docker-compose.yml) |
| [portainer](portainer/) | Container management | [Compose](portainer/docker-compose.yml) |
| [portainer-edge-agent](portainer-edge-agent/) | Portainer edge agent | [Compose](portainer-edge-agent/docker-compose.yml) |
| [qbittorrent](qbittorrent/README.md) | Torrent client | [Compose](qbittorrent/docker-compose.yml) |
| [searxng](searxng/) | Metasearch engine | [Compose](searxng/docker-compose.yml) |
| [strata](strata/README.md) | Source-built Qwen model inference | [Compose](strata/docker-compose.yml) |
| [traefik](traefik/README.md) | Reverse proxy and TLS | [Compose](traefik/docker-compose.yml) |
| [tts-chatterbox](tts-chatterbox/README.md) | Multilingual ROCm speech API | [Compose](tts-chatterbox/docker-compose.yml) |
| [tts-qwen](tts-qwen/README.md) | Qwen3-TTS speech API | [Compose](tts-qwen/docker-compose.yml) |
| [ttyd](ttyd/) | Terminal in a browser | [Compose](ttyd/docker-compose.yml) |
| [vaultwarden](vaultwarden/README.md) | Bitwarden-compatible vault server | [Compose](vaultwarden/docker-compose.yml) |
| [vllm](vllm/) | GPU model serving | [Compose](vllm/docker-compose.yml) |
| [wud](wud/README.md) | Container image update dashboard | [Compose](wud/docker-compose.yml) |

## Manual deployment

```bash
git clone https://github.com/jin-li/selfhost.git
cd selfhost/<service>
```

Read the service guide, copy its example configuration where provided, and
set your own paths, credentials, network names, and bind addresses. Inspect
all required variables and mounts before running:

```bash
docker compose config --quiet
docker compose up -d
docker compose ps
```

Use the same environment and override files for subsequent commands. Keep
credentials, generated environment files, and persistent application state
outside Git. For stateful stacks, back up databases and volumes before updates.

Some stacks require explicit build or GPU overrides; their guides provide the
matching commands. Create required external networks before deploying proxy
stacks. Cloudflare Tunnel requires your own account and tunnel configuration;
local-only services do not require a domain or tunnel.

## Service manifests

A `container.yaml` file is optional integration metadata for deployment tooling.
It describes Compose files, inputs, generated files, persistent state, health
checks, and placement. See the [manifest reference](docs/container-manifest.md).
Manifests do not start or stop services.

## Contributing

Keep Compose bases, guides, and examples portable. Use generic domains, paths,
and host labels. Do not add real host placement, user paths, tunnel identifiers,
network topology, credentials, or links to private deployment repositories.
Installation-specific overlays and operator runbooks belong outside this public
collection. Keep this stack index alphabetized when adding a service.
