# Font sources

Both families are SIL Open Font License 1.1; the license texts sit
beside the files (`OFL-Fraunces.txt`, `OFL-JetBrainsMono.txt`).

The woff2 files are the per-subset variable builds Google Fonts serves
a current Chromium for the axes `templates/base.html` used to request:

    https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght,SOFT,WONK@9..144,300..900,0..100,0..1&family=JetBrains+Mono:wght@400;500;600&display=swap

fetched with the UA `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)
AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36`.
`static/css/fonts.css` carries the same `@font-face` descriptors,
`unicode-range` included, with local URLs.

| file | Google Fonts URL | sha256 |
| --- | --- | --- |
| `fraunces-latin.woff2` | `https://fonts.gstatic.com/s/fraunces/v38/6NUV8FyLNQOQZAnv9ZwIlOkuy91B.woff2` | `94bb7e04bb1a32237a67935c72526e42a3e52ee4aebd50299802b18a93114251` |
| `fraunces-latin-ext.woff2` | `https://fonts.gstatic.com/s/fraunces/v38/6NUV8FyLNQOQZAnv9ZwGlOkuy91BRtw.woff2` | `bfbcc574c88f5008f497dd150b7d043db484b5d5866c949b1c6e64f50a085ad8` |
| `fraunces-vietnamese.woff2` | `https://fonts.gstatic.com/s/fraunces/v38/6NUV8FyLNQOQZAnv9ZwHlOkuy91BRtw.woff2` | `bc5c1f2dca610837c6a11a164ae3cc7ed51b0a0d209eb0cbf705d7f63e87a2c4` |
| `jetbrains-mono-latin.woff2` | `https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPxDcwgknk-4.woff2` | `2c32b9b3ee358c119e210f6f5195f9bd34894d78a785ff2e95d60e718e400af4` |
| `jetbrains-mono-latin-ext.woff2` | `https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPx7cwgknk-6nFg.woff2` | `9c38cb2d0d2d93c1ee6e21fa78db76f13ea7e15e15cc64214c7ca89b6aaa35c4` |
| `jetbrains-mono-greek.woff2` | `https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPxPcwgknk-6nFg.woff2` | `49c3da6c9a2b279b0f1f860f5cfb1f5dc38d88a5c7be9c9b1837bbc4e3db6111` |
| `jetbrains-mono-cyrillic.woff2` | `https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPxTcwgknk-6nFg.woff2` | `4995a9a43ac659ec32fcd8b463755cd6a07b31a6e6b3894a6a153b661cf490e2` |
| `jetbrains-mono-cyrillic-ext.woff2` | `https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPx3cwgknk-6nFg.woff2` | `9343de2ca5d9549f792e7962375af8efb0f320c7643bfd36c884b5a30e5c396f` |
| `jetbrains-mono-vietnamese.woff2` | `https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPx_cwgknk-6nFg.woff2` | `d44eb1936043a56038eb02dd70b243f379bef65783f94ec12f277550720411f1` |

Upstream projects: undercasetype/Fraunces (Google build v38),
JetBrains/JetBrainsMono (Google build v24).
