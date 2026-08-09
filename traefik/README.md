# traefik

## Introduction

Traefik is a modern reverse proxy and load balancer that makes deploying microservices easy. It automatically discovers services and routes traffic to them based on configuration.

## Usage

0. Create `.env` and `traefik.yml` files based on the provided templates.
1. In `.env`, set the Cloudflare API token and email for authentication.
2. In `traefik.yml`, set the certificate resolver for TLS.
3. Create `dynamic` folder based on `dynamic.example` for hosting the file-based dynamic configurations. Add your desired routes and services in the `dynamic` folder. You can refer to the example configuration files in `dynamic.example` for guidance.
4. Run `docker compose up -d` to deploy the container.

## Note

- After adding a new dynamic configuration file, you don't need to restart the Traefik container. Traefik automatically detects changes in the `dynamic` folder and reloads the configuration.