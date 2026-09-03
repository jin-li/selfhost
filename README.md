# Self-hosted Docker Apps

This repository contains portable Docker Compose stacks for self-hosted
applications. Each application has its own directory with a
`docker-compose.yml` base file, any example configuration it needs, and — where
the stack is deployment-managed — a `container.yaml` service manifest.

The stacks use Cloudflare Tunnel and Traefik. With a domain name hosted on
Cloudflare you can reach these applications from anywhere without exposing your
home network.

## Directory layout

```text
<service>/
├── container.yaml        # service manifest (schema 1), where present
├── docker-compose.yml    # portable Compose base
└── config.example.*      # configuration templates (no real values)
```

Only templates and examples are committed. Generated configuration, `.env`
files, credentials, and application data stay out of the repository (see
`.gitignore`).

## Service manifests

A `container.yaml` manifest (schema 1) declares everything a deployment
renderer needs: Compose base files and optional host overlay, environment
variables and their sources, generated files with modes, required networks,
persistent-data declarations with backup notes, health checks, allowed hosts
and placement mode, and legacy compatibility paths. See the
[manifest reference](docs/container-manifest.md) for the full format.

On managed hosts, `containerctl` (shipped with the private `nixos-config`
repository) resolves each manifest against the host inventory and SOPS-
encrypted sources, renders environment and files into a private runtime tree
that is recreated at boot, and runs Compose from the companion `containers`
repository: base file(s) from this repo plus the per-host overlay, with the
rendered values mounted in. Manifests never contain credentials; secret values
are referenced by scope and path and resolved at render time.

## Manual usage (unmanaged hosts)

For hosts not driven by `containerctl`, deploy a stack directly:

1. **Preparation**: run Docker in rootless mode if your distro supports it
   ([Docker documentation](https://docs.docker.com/engine/security/rootless/))
   and host your domain on Cloudflare.

2. **Clone the repository**:

   ```bash
   git clone https://github.com/jin-li/selfhost.git
   ```

3. **Create configuration** from the `*.example` templates in the service
   directory (environment file, tunnel credentials, routes).

4. **Set up the reverse proxy**:

   a. Cloudflare Tunnel — ingress routes for the services you expose.
   b. Traefik — dynamic route files; a shared middleware adds the
      `X-Forwarded-Proto: https` header so applications behind Traefik see the
      original request protocol.

5. **Deploy** with `docker compose up -d`.
