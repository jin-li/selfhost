# Service manifest reference (schema 1)

A `container.yaml` file next to a service's Compose base declares everything a
deployment renderer needs to know about the stack. Manifests are data, not
lifecycle control: the renderer validates them and renders configuration, but
never starts, stops, or restarts containers.

All paths in a manifest are relative to the repository root unless stated
otherwise; no path may escape it (`..` segments are rejected).

## Top-level fields

| Field | Type | Meaning |
|-------|------|---------|
| `schema` | `1` | Manifest format version. |
| `service` | name | Canonical service name; keys the host inventory and runtime tree. |
| `projectName` | name | Docker Compose project name (container prefix). |
| `compose` | object | Which files build the deployment (below). |
| `environment` | map | Environment variables for the containers (below). |
| `files` | map | Generated files rendered into the runtime tree (below). |
| `networks` | object | `{ required: [names] }` — networks the service must join. |
| `data` | object | `{ declarations: [...] }` — persistent state (below). |
| `validation` | object | `{ healthChecks: [...] }` — how `doctor` verifies the service. |
| `placement` | object | Where and how the service may run (below). |
| `autostart` | boolean | Whether the host starts the service automatically. |
| `compatibilityPaths` | paths | Legacy configuration locations a migration must clear or migrate; the renderer fails if they still exist. |

Names match `^[A-Za-z0-9][A-Za-z0-9_.-]*$`.

## Compose (`compose`)

```yaml
compose:
  root: selfhost/<service>
  base: [docker-compose.yml]
  hostOverlay: local/{host}/<service>/compose.{host}.yaml   # or null
```

- `root` — directory the deployment runs from.
- `base` — portable Compose file(s) in this repository, merged in order.
- `hostOverlay` — optional per-host override file in the companion `containers`
  repository; `{host}` is replaced with the target host name. `null` means the
  service needs no host-specific Compose changes.

## Environment (`environment`)

Each entry names a container environment variable and declares its `source`:

- **Inventory path** — `global.<path>` or `host.<path>`: resolved from the
  global values file and the host inventory in `nixos-config` (non-secret).
- **Rendered file** — `runtime.files.<name>`: the runtime path of a file
  declared under `files` (for example, a mounted credentials file).
- **Secret reference** — an object with `scope`, `path`, and optional `key`:

  ```yaml
  CF_API_KEY:
    source:
      scope: host-service     # global | service | host | host-service
      path: env.yaml          # SOPS-encrypted file in nixos-config/secrets/
      key: CF_API_KEY         # key inside the file (default: whole file)
      format: yaml            # yaml | json | text | dotenv
    secret: true
  ```

  `scope` selects the encryption scope: `global` (all hosts), `service`
  (portable between hosts), `host`, or `host-service` (one service on one
  host). Choose the narrowest scope that fits.

Other entry options: `type` (`string`, `integer`, `boolean`), `default`,
`required`/`optional` (mutually exclusive), and `secret` (marks the value as a
credential for display and handling). Secret-sourced entries must not define a
`default`. Missing required values fail rendering instead of producing a broken
deployment.

## Files (`files`)

Each entry renders one file into the service's runtime directory, where Compose
bind-mounts it:

```yaml
credentials:
  source:
    scope: host-service
    path: files/credentials.json
    format: text
  target: credentials.json
  secret: true
  required: true
  mode: "0444"
```

- `source` — secret reference (above); `format: text` copies the file as-is.
- `target` — path inside the service runtime directory.
- `mode` / `owner` / `group` — permissions for the rendered file, chosen so the
  container's non-root identity can read it.

The runtime tree is host-private and recreated at boot; rendered files must
never be copied back into a repository.

## Data (`data.declarations`)

Each declaration names a piece of persistent state:

```yaml
- name: acme-state
  source: host.services.traefik.acmePath   # inventory path holding the host location
  containerPath: /letsencrypt/acme.json
  backupNotes: Stop Traefik before copying ACME account and certificate state.
```

`containerPath` is absolute inside the container; `backupNotes` records what a
safe backup or migration must do (stop order, consistency requirements).
Application data always lives outside Git.

## Validation (`validation.healthChecks`)

Each check has a `name` plus exactly one of:

- `http`: `{ url, expectedStatuses }` — an endpoint the service must answer.
- `command`: `[argv...]` — a command that must succeed inside a container.

`doctor` runs checks against the live deployment and reports health without
changing it.

## Placement (`placement`)

```yaml
placement:
  allowedHosts: [halo, surf, t460s]   # hosts where the service may run
  mode: singleton                     # singleton | active-passive
```

A service deploys to a host only when the manifest allows it **and** the host
inventory enables it. `singleton` means at most one host runs it;
`active-passive` allows one active and one standby host.
