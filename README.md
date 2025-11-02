# website_cloner

Clone a page or mirror a site to a local folder.
HTML is rewritten to use local assets. Output is structured and deterministic.
Polite throttling avoids “You’re making too many requests” errors.

---

## Features

* Single‑page clone or BFS crawl with `--max-depth` and `--max-pages`.
* Retries with backoff. Honors `Retry-After`. Global and per‑host RPS with jitter.
* Optional `robots.txt` respect and sitemap seeding.
* URL normalization. Drops tracking params by default. Whitelist allowed params.
* Asset coverage: `<img>`, `<source>`, `<video poster>`, `<link rel=*>`, `<script>`, `srcset`, inline `style`, `<style>`, `@import`, `url(...)`.
* Link rewriting to local pages.
* Output layout groups assets by type and separates third‑party “vendor” hosts.
* Optional headless render with Playwright to capture client‑rendered HTML.
* Manifest file with page and asset list.

---

## Requirements

* Python 3.9+
* Dependencies:

  ```bash
  pip install requests beautifulsoup4 urllib3
  # Optional for JS rendering:
  pip install playwright
  playwright install
  ```

---

## Install

```bash
git clone <your-repo-url> website_cloner
cd website_cloner
pip install -r requirements.txt  # if you provide one
# or install packages as shown above
```

`website_cloner.py` is a single script. No services required.

---

## Usage

### Quick start

```bash
# Bash / zsh
python website_cloner.py https://example.com out
```

```powershell
# PowerShell
python .\website_cloner.py https://example.com .\out
```

Open `out/example.com/pages/index.html` in a browser.

### Crawl multiple pages

```bash
# Bash / zsh
python website_cloner.py https://example.com out \
  --crawl --max-depth 2 --max-pages 300
```

```powershell
# PowerShell uses backtick for line continuation (not backslash)
python .\website_cloner.py https://example.com .\out `
  --crawl `
  --max-depth 2 `
  --max-pages 300
```

### Polite throttling

Lower concurrency and RPS for strict sites:

```bash
python website_cloner.py https://example.com out \
  --crawl --max-depth 1 --max-pages 50 \
  --workers 2 --per-host-rps 0.5 --global-rps 0.5 \
  --delay 0.5 --respect-robots
```

### Headless JS rendering (optional)

```bash
pip install playwright && playwright install
python website_cloner.py https://example.com out --render-js
```

Rendering blocks images, media, and fonts during navigation by default to reduce requests. Disable with `--no-render-block-media`.

---

## Output structure

```
out/
└─ <host>/
   ├─ pages/
   │  └─ .../index.html         # one file per page URL
   ├─ assets/
   │  ├─ css/                   # first‑party CSS
   │  ├─ js/                    # first‑party JS
   │  ├─ img/                   # first‑party images and icons
   │  ├─ font/
   │  ├─ media/                 # audio / video
   │  ├─ data/                  # JSON, webmanifest, sourcemaps
   │  ├─ other/                 # uncategorized
   │  └─ vendor/
   │     └─ <external-host>/
   │        ├─ css/ js/ img/ ...
   └─ meta/
      └─ manifest.json          # pages and assets inventory
```

* Filenames include an 8‑char hash of the full URL: `name_<hash>.ext`.
  This avoids collisions when query strings differ.
* **Pages** are always under `pages/` and keep directory structure, ending in `index.html`.
* **Assets**

  * Same‑origin assets go under `assets/<type>/`.
  * Third‑party assets go under `assets/vendor/<host>/<type>/`.
* Assets are downloaded regardless of host. Page crawling is same‑origin by default.

---

## Manifest

`meta/manifest.json` captures what was saved:

```json
{
  "site": "https://example.com",
  "created_utc": "2025-01-01T00:00:00Z",
  "pages": ["pages/index.html", "pages/blog/index.html"],
  "assets": [
    "assets/css/main_1a2b3c4d.css",
    "assets/vendor/cdn.example.com/js/app_5e6f7a8b.js"
  ],
  "layout": {
    "pages_dir": "pages/",
    "assets_dir": "assets/",
    "vendor_dir": "assets/vendor/"
  },
  "notes": "Assets grouped by type. Third-party assets under vendor/<host>/."
}
```

---

## CLI options

```
positional:
  url                     http(s) URL
  output_folder           output directory

crawl:
  --crawl                 crawl links and mirror multiple pages
  --max-pages N           cap on HTML pages (default 100)
  --max-depth N           BFS depth from start URL (default 1)
  --include REGEX         only crawl URLs matching pattern
  --exclude REGEX         skip URLs matching pattern
  --sitemap-seed          use Sitemap URLs as extra seeds
  --no-strip-params       keep all query parameters
  --allow-param K         repeatable param name to keep when stripping

render:
  --render-js             render with Playwright if installed
  --render-timeout-ms N   navigation timeout (default 10000)
  --wait-until EVENT      load | domcontentloaded | networkidle
  --no-render-block-media do not block images/fonts during render

network:
  --timeout SEC           request timeout (default 15)
  --workers N             concurrent asset downloads (default 4)
  --max-bytes N           per‑file size cap (default 50,000,000)
  --skip-js               do not download <script src> assets
  --respect-robots        honor robots.txt and crawl-delay

throttle:
  --global-rps R          global requests/sec (default 2.0)
  --per-host-rps R        per‑host requests/sec (default 1.0)
  --burst N               token bucket burst size (default 2)
  --delay SEC             base delay per request (default 0)
  --jitter F              extra random delay fraction 0..1 (default 0.3)
  --max-backoff SEC       cap for 429/503 backoff (default 60)
  --no-auto-throttle      disable adaptive backoff

general:
  --external              allow cross‑origin pages (assets are always fetched)
  --verbose               debug logging
```

---

## How it works

1. Fetch HTML (Requests or Playwright).
2. Parse and collect asset URLs and anchors.
3. Download assets with retries, backoff, token‑bucket throttling, and size caps.
4. Rewrite HTML:

   * `src`, `href`, `poster`, `srcset`
   * inline `style` and `<style>` `url(...)` and `@import`
   * internal `<a href>` to local `pages/...`
5. Save HTML to `pages/.../index.html`.
6. Optionally crawl BFS and repeat.
7. Write `meta/manifest.json`.

---

## Tips

* **PowerShell line breaks**: use the backtick `` ` `` or splatting. Backslash `\` is for Bash.
* **Gentle mode** for strict sites:

  ```
  --workers 2 --per-host-rps 0.5 --global-rps 0.5 --delay 0.5 --respect-robots
  ```
* **Limit bandwidth**: `--skip-js`, lower `--max-bytes`, keep `--render-js` with media blocked.
* **Keep key params**: `--allow-param page --allow-param id`.
* **Serve locally**:

  ```bash
  cd out/example.com
  python -m http.server 8080
  # browse http://localhost:8080/pages/index.html
  ```

---

## Limitations

* Not a full browser unless `--render-js` is enabled. Some dynamic content may be missing.
* Service workers, WebSockets, live APIs, and authenticated areas are out of scope.
* Forms and interactive logic may not work offline.
* Very large media files are skipped or truncated by `--max-bytes`.
* CSP and SRI attributes are removed from tags that are rewritten.

---

## Troubleshooting

* **PowerShell “Missing expression after unary operator ‘--’”**
  Use backticks for line continuation or splatting. Example shown above.

* **429/“too many requests”**
  Lower `--workers`, `--per-host-rps`, and `--global-rps`. Add `--delay`. Keep `--respect-robots`.

* **Blank page**
  Try `--render-js`. Increase `--render-timeout-ms`. Disable media blocking if needed.

* **External assets not desired**
  Assets are always fetched and placed under `assets/vendor/<host>/`. To restrict, use `--skip-js` and content filters (e.g., edit `extract_asset_urls`), or block hosts at the network layer.

* **Paths too long (Windows)**
  Use a shorter `out` path (e.g., `C:\out`). Filenames are already compact with an 8‑char hash.

---

## Legal

Only clone content you own or have explicit permission to copy.
Respect `robots.txt` when appropriate (`--respect-robots`).

---

## License

Add your preferred license here.
