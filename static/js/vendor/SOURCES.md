# Vendored scripts

Third-party bundles served from this origin rather than a CDN, so no other
host can change what runs on the page. Each file is pinned to an exact
version, copied unmodified from the package tarball on the npm registry (the
tarball checked against the registry's `dist.integrity`). To upgrade, fetch the
new tarball with `npm pack <name>@<version>`, copy the same files under
new versioned names, and update the references and this table.

| File | Package | From the tarball |
| --- | --- | --- |
| `htmx-2.0.4.min.js` | `htmx.org@2.0.4` | `dist/htmx.min.js` |
| `swagger-ui-bundle-5.35.0.js` | `swagger-ui-dist@5.35.0` | `swagger-ui-bundle.js` |
| `swagger-ui-5.35.0.css` | `swagger-ui-dist@5.35.0` | `swagger-ui.css` |
| `redoc-2.5.4.standalone.js` | `redoc@2.5.4` | `bundles/redoc.standalone.js` |

Licenses: `LICENSE-swagger-ui` (Apache-2.0), `LICENSE-redoc` (MIT), and
`swagger-ui-bundle-5.35.0.js.LICENSE.txt` for the bundled dependencies.
