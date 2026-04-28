# System-level dependencies for developing prep on macOS.
# Run `brew bundle` in this directory to install everything.
#
# mise (https://mise.jdx.dev/) is the primary toolchain manager — it
# reads .tool-versions and provisions Python, Go, and Bun at the pinned
# versions. The Brewfile only handles binaries that mise can't easily
# manage from its plugin registry.
#
# Linux contributors: see CONTRIBUTING.md (one-line mise installer +
# apt-equivalent for temporal).

# Required:
brew "mise"          # toolchain manager — provides python/go/bun per .tool-versions
brew "temporal"      # local devserver — backs both prod and dev workflows

# Optional:
# brew "caddy"       # path-prefixed reverse proxy. Only needed for
#                    # prod-shape local testing behind /prep/ — `make dev`
#                    # binds bare localhost without it.
# brew "pm2"         # process supervisor; alternative to goreman for prod-
#                    # shape testing. `make dev` uses goreman.
