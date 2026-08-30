# Headscale

## Docker Deployment

1. Clone the repository.

2. Set up the environment variables by copying the example files.

    There are two sets of configuration files: one with OIDC enabled and one without. Choose the appropriate set based on your authentication requirements. The example below assumes OIDC is enabled.

    - If you choose to use OIDC, copy the OIDC-enabled example configuration files:

        ```bash
        cp .env.example .env
        cp ./headscale/config/config.example.yaml ./headscale/config/config.yaml
        cp ./headplane/config/config.example.yaml ./headplane/config/config.yaml
        ```

    - If you choose not to use OIDC, copy the non-OIDC example configuration files:

        ```bash
        cp .env.no-oidc.example .env
        cp ./headscale/config/config.no-oidc.example.yaml ./headscale/config/config.yaml
        cp ./headplane/config/config.no-oidc.example.yaml ./headplane/config/config.yaml
        ```
    
    If you are using OIDC, make sure to create the necessary OIDC clients in your authentication provider (e.g., Authelia) for both Headscale and Headplane.

3. Make configurations. 
    - Edit the `.env` file to set the data storage path.
    - Edit the `headscale/config/config.yaml` file to configure for Headscale. In the example setup, OIDC is enabled and Authelia is used as the authentication provider. An OIDC client must be created in Authelia for Headscale.
    - Edit the `headplane/config/config.yaml` file to configure for Headplane. In the example setup, OIDC is enabled and Authelia is used as the authentication provider. An OIDC client must be created in Authelia for Headplane.

4. Build the Docker images:

    ```bash
    docker compose up -d
    ```