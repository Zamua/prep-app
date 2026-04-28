# System-level dependencies for developing prep on macOS.
# Run `brew bundle` in this directory to install everything.
#
# Linux contributors: equivalent packages on apt are
#   python3, python3-venv, golang-go, bun (via apt repo or curl install),
#   temporal-cli (via github releases). See CONTRIBUTING.md.

# Required for `make dev`:
brew "python@3.11"
brew "go"
brew "temporal"      # local devserver — backs both prod and dev workflows
brew "bun"           # only needed if you rebuild the CodeMirror bundle
brew "goreman"       # Procfile runner used by `make dev`

# Optional for prod-shape local testing:
# brew "caddy"       # path-prefixed reverse proxy; only needed if you want to
#                    # run the app behind /prep/ instead of bare localhost
# brew "pm2"         # process supervisor; alternative to goreman if you prefer
#                    # pm2's monitoring tools
