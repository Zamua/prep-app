# System-level dependencies for developing prep on macOS.
# Run `brew bundle` in this directory to install everything.
#
# Linux contributors: equivalent paths in CONTRIBUTING.md (curl-install
# uv, apt-install go, etc.).

# Required for `make dev`:
brew "uv"            # Python toolchain — installs Python itself + venv + deps
brew "go"            # worker-go build
brew "temporal"      # local devserver — backs both prod and dev workflows
brew "goreman"       # Procfile runner used by `make dev`

# Optional:
# brew "bun"         # only needed if you rebuild the CodeMirror bundle.
#                    # The built bundle ships in static/cm-bundle.js so most
#                    # contributors don't need bun.
# brew "caddy"       # path-prefixed reverse proxy. Only needed for
#                    # prod-shape local testing behind /prep/ — `make dev`
#                    # binds bare localhost without it.
# brew "pm2"         # process supervisor; alternative to goreman for prod-
#                    # shape testing. `make dev` uses goreman.
