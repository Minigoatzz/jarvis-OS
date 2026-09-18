# Mapbox as a tool (not just a view) — plan

Status: **planned, not started.** Raised 2026-09-18.
Feasibility: `Moyen` (new L1 tool + server proxy + one storage decision).

## Idea

Today Mapbox is a *rendering dependency*: `MAPBOX_TOKEN` feeds tiles to the
globe view, and `map_control` only moves the camera. The token buys far more
than tiles — Directions, Matrix and Optimization are on the same account.

The goal is to make travel time a **fact Jarvis can reason with**, available to
any part of the system, not a picture on one screen:

- "how long to get to school right now" — answered in chat, no view needed;
- calendar events get a **leave-by time** derived from real drive time;
- a road trip becomes a multi-waypoint route with per-leg durations;
- the globe *optionally* draws the result, as one consumer among others.

## What already exists

| Piece | State |
|---|---|
| `MAPBOX_TOKEN` | in `.env`, editable from Settings → Clés API, hot-applied |
| Globe tiles | `globe.js` sets `mapboxgl.accessToken` from `/api/globe/config` |
| Camera control | `map_control` — `fly_to`, `zoom_in/out`, `globe_view`, `toggle_panels` |
| Geocoding | `map_control._geocode` — 27 hardcoded cities, then **Nominatim** |
| Calendar | `proactive/collectors/calendar.py` + `list_calendar_events` tool |

## What's missing

1. **No Directions call anywhere.** Only `open-meteo` and `opensky-network` are
   contacted by the globe API.
2. **No saved places.** Nothing knows "my house". Needs a decision (below).
3. **No route layer.** `map_control` broadcasts camera events only; there is no
   GeoJSON source or line layer in `view.js` to draw a path.
4. **No hook from calendar to travel time.**

## Proposed shape

### 1. `capabilities/tools/maps.py` → tool `maps_route`

```
origin, destination : str   (place name, saved place, or "ici")
mode                : driving | walking | cycling   (default driving)
depart_at           : ISO8601, optional (traffic-aware profile)
show_on_globe       : bool, default false
```

Returns duration, distance and a short leg summary as **text the model can
speak**. When `show_on_globe` is true it also broadcasts a `map_route` event
carrying the GeoJSON — same pattern `map_control` already uses for `map_fly_to`.

Register it in `bootstrap.py` (`tests/test_tools_wired.py` enforces this).

### 2. Server-side proxy, not a browser call

Directions goes through the backend so the token stays server-side. The globe
already receives the token for tiles — that's unavoidable — but there's no
reason to widen its exposure to routing quota too.

### 3. Saved places — decision needed

Three options, roughly increasing effort:

- **memory topic** (`topics/lieux.md`) — free-form, Jarvis can already write it,
  but resolution is fuzzy and depends on the LLM reading it;
- **`config/places.json`** — explicit, editable from Settings like the API keys;
- **env vars** (`HOME_ADDRESS`, …) — simplest, least flexible.

Recommendation: `config/places.json`, because drive times should not silently
depend on whether the model recalled the right address.

### 4. Calendar drive time

Once `maps_route` exists, the calendar collector computes leave-by for events
carrying a location. This is where the feature actually pays off day to day —
worth doing before the road-trip case.

### 5. Road trip

Directions takes up to 25 waypoints in a fixed order. **Reordering** them
optimally is a different product (Optimization API) — treat it as a separate
decision, not a free extra.

## Watch-outs

- **Two geocoders would disagree.** `_geocode` uses Nominatim; Directions would
  naturally use Mapbox geocoding. Mixing them means "Longueuil" can resolve to
  two different points depending on the code path. Pick one before building.
- **Nominatim caps at ~1 req/s** and its failures currently surface as "Lieu
  introuvable" (the `except Exception` in `_geocode` swallows the cause). Fix
  that first if routing is going to depend on geocoding.
- **Quota.** Mapbox free tier is generous but finite, and a calendar collector
  firing per event is a recurring cost, not a one-off. Cache by
  (origin, destination, mode, hour).
- **The globe is the wrong surface for live guidance.** A drawn line and a step
  list, yes. Turn-by-turn while driving, no.

## Order of work, when picked up

1. Settle the geocoder (and fix `_geocode`'s error swallowing).
2. `maps_route` tool + server proxy + tests. Useful on its own, in chat.
3. Saved places.
4. Calendar leave-by.
5. Route layer on the globe.
6. Road trip / multi-waypoint.

Steps 1–2 deliver value alone; everything after is additive.
