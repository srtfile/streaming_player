# Stream Player + MAL Direct backend

This bundle has two parts:

- **`index.html`** — the player. It now has an extra collapsible panel,
  **"🎬 MAL Direct search"**, in the Sources column. Search an anime by name
  (or type a MAL ID directly) and an episode number to pull sub/dub/hsub
  server links straight from the shared anime index — this is the browser
  port of `anime_stream_finder.py`.
- **`server/app.py`** — an optional FastAPI backend that does the same
  lookup server-side with in-memory caching, so repeat/concurrent requests
  are fast instead of everyone re-downloading the full `index.json` on every
  click. Deploy it to Render straight from this GitHub repo.

## 1. Deploy the backend to Render

1. Push this folder to a GitHub repo.
2. In Render: **New → Blueprint**, point it at the repo — `render.yaml` at
   the root configures everything automatically (root dir `server/`, build
   command, start command, health check).
   - Or manually: **New → Web Service**, root directory `server`,
     build command `pip install -r requirements.txt`,
     start command `uvicorn app:app --host 0.0.0.0 --port $PORT`.
3. Once deployed you'll get a URL like `https://mal-stream-finder.onrender.com`.
4. Check `https://<your-url>/health` returns `{"status":"ok",...}`.

## 2. Wire the frontend to it

Open `index.html` and set:

```js
const MAL_BACKEND_BASE = 'https://mal-stream-finder.onrender.com'; // your Render URL
```

(It's currently `''`, which makes the page fall back to fetching
`index.json` and each title's episode file directly from
`raw.githubusercontent.com` in the browser — works fine, just slower on the
first search since there's no server-side cache.)

## 3. Using it

- Open the **🎬 MAL Direct search** panel in the Sources column.
- Type an anime name and hit **Search**, or type numbers into **MAL ID**
  directly if you already know it.
- Set the **Episode** number and click **⚡ Get Streams**.
- Results (iframe links + direct `.m3u8`s, grouped by server and
  sub/dub/hsub) show up in the stream list tagged **MAL Direct** — click any
  to play, or filter to just that source with the **🎬 MAL Direct** tab.

## API reference (backend)

- `GET /api/search?q=<name>&limit=25` → `{"results":[{"mal_id":21,"anime_name":"One Piece","anime_url":"..."}]}`
- `GET /api/stream?mal_id=21&episode=1` →
  ```json
  {
    "mal_id": 21,
    "anime_name": "One Piece",
    "anime_url": "...",
    "episode": 1,
    "episode_title": "...",
    "episode_url": "...",
    "streams": {"sub": {"server": {"m3u8_key": "https://...", "iframe": "https://..."}}, "dub": {...}},
    "m3u8": ["https://...", "..."]
  }
  ```

## Notes on speed

- The index (`index.json`) is cached in memory for 30 minutes per backend
  instance; per-title episode files are cached for 5 minutes.
- Render's free plan spins the service down after inactivity, so the very
  first request after idle time will be slow (cold start) — subsequent ones
  are fast thanks to the cache.
