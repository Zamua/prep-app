# System-level dependencies for developing prep on macOS.
# Run `brew bundle` in this directory to install everything.
#
# mise (https://mise.jdx.dev/) is the toolchain manager — it reads
# .tool-versions and provisions Python, Go, Bun, and Temporal at the
# pinned versions. The Brewfile only installs mise itself.
#
# Linux contributors: see CONTRIBUTING.md (one-line mise installer).

# Required:
brew "mise"          # toolchain manager — provides python/go/bun/temporal per .tool-versions

# Optional:
# brew "caddy"       # path-prefixed reverse proxy. Only needed for
#                    # prod-shape local testing behind /prep/ — `make dev`
#                    # binds bare localhost without it.
# brew "pm2"         # process supervisor; alternative to goreman for prod-
#                    # shape testing. `make dev` uses goreman.
