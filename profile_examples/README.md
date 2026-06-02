# Example investigation profiles

Ready-made search packages for the Podcast Tracker. Each is a self-contained
investigation (its own DB, queries, channels, keywords, and analysis framing).

| File | Topic |
|---|---|
| `seo-geo.json` | SEO / AI / GEO (the built-in default) |
| `zone2-training.json` | Zone 2 / aerobic-base endurance training |

## How to use one

These are reference copies. The live profiles the app reads live in
`~/.hermes/profiles/`. To load an example:

```bash
cp profile_examples/zone2-training.json ~/.hermes/profiles/
python3 profiles.py use zone2-training        # make it active
python3 dashboard_server.py --migrate          # create its DB
python3 podcast_scraper.py --test              # preview reach (no writes)
python3 podcast_scraper.py                      # run discovery
```

Or, from the dashboard, use the **Investigation** dropdown → **+ New** to build
one in the browser (with a Test-preview button), then **Run Discovery**.

`zone2-training.json` was validated with `--test`: ~275 videos across ~107
channels, with the 11 curated authorities (Peter Attia, Dylan Johnson, GCN,
Fast Talk Labs, Floris Gierman, …) plus keyword-discovered creators like Steve
Magness.
