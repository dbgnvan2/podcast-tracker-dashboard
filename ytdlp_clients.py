#!/usr/bin/env python3
"""Single source of truth for the yt-dlp player-client selection.

Purpose: One client decision, shared by the transcript fetcher and the caption
         availability probe, so the probe can never read through a *different*
         (PO-token-gated) client than the fetcher and silently under-report
         captions (LEARNINGS P5; DESIGN-transcript-availability.md §4 Phase 2,
         §5 "Phase 2 blocks Phase 3").
Spec:    DESIGN-transcript-availability.md#phase-2
Tests:   test_app.py::TestPlayerClients

The VALUE below is **provisional** — `["android", "web"]`, the historical pair.
`spike_clients.py` measures which client set actually returns captions on live
videos (network-dependent, run outside a 429 cooldown); the winner is recorded in
DESIGN-transcript-availability.md / LEARNINGS.md and set here. A client value that
has not been measured is a guess (that doc is explicit) — change PLAYER_CLIENTS
only from a committed measurement, never by intuition.
"""

# Ordered client preference. yt-dlp's `player_client` extractor arg accepts a
# comma-separated list and tries them in order.
PLAYER_CLIENTS = ["android", "web"]


def player_client_arg(clients=None):
    """The `--extractor-args` VALUE selecting the client preference, e.g.
    ``youtube:player_client=android,web``. Pass it to yt-dlp immediately after
    ``--extractor-args``. Defaults to the shared PLAYER_CLIENTS so every caller
    inherits the one decision."""
    cl = list(clients) if clients is not None else PLAYER_CLIENTS
    return "youtube:player_client=" + ",".join(cl)
