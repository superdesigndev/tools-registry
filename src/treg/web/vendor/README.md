# vendor/

Third-party front-end files, committed rather than fetched from a CDN at page load.

The dashboard (`../index.html`) is one hand-written Vue file with no bundler, so Vue arrives as a
plain `<script src>`. When that src pointed at `unpkg.com`, every visitor whose network could not
reach unpkg got a blank signed-in dashboard and no error — [#137](https://github.com/superdesigndev/treg/issues/137).
Served from here, the dashboard depends on exactly one origin: whoever served the page also serves
its runtime. It also retires a floating `vue@3` tag, which had let whatever npm published next
execute inside an authenticated session.

Mounted at `/vendor` by `api.py`; shipped in the wheel because `web/` sits inside the package.

## Contents

| File | Version | License | Upstream |
|---|---|---|---|
| `vue-3.5.41.global.prod.js` | 3.5.41 | MIT | `https://unpkg.com/vue@3.5.41/dist/vue.global.prod.js` |

## Bumping a version

Filenames carry their version, so a stale cache can never shadow a new file — and the version is
visible at the point of use instead of buried in a lockfile.

```bash
V=3.6.0
curl -sSLo src/treg/web/vendor/vue-$V.global.prod.js https://unpkg.com/vue@$V/dist/vue.global.prod.js
# verify the bytes against a second, independent CDN before trusting them:
curl -sSLo /tmp/vue-check.js https://cdn.jsdelivr.net/npm/vue@$V/dist/vue.global.prod.js
cmp /tmp/vue-check.js src/treg/web/vendor/vue-$V.global.prod.js && echo OK
```

Then update the `<script src>` in `../index.html`, delete the old file, and update the table above.
Pin an exact version — never a floating range like `vue@3`.
