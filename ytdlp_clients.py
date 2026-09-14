#!/usr/bin/env python3
"""Single source of truth for the yt-dlp player-client selection.

Purpose: One client decision, shared by the transcript fetcher and the caption
         availability probe, so the probe can never read through a *different*
         (PO-token-gated) client than the fetcher and silently under-report
         captions (LEARNINGS P5; DESIGN-transcript-availability.md §4 Phase 2,
         §5 "Phase 2 blocks Phase 3").
Spec:    DESIGN-transcript-availability.md#phase-2
Tests:   test_app.py::TestPlayerClients

The VALUE below is **measured**, not a guess. `spike_clients.py` (Part 2 of
DESIGN-transcript-availability.md) A/B'd six client sets on 12 videos with yt-dlp
2026.08.19 (2026-09-14): android,web / android / default,-web each hit **100%**;
tv / ios / tv,ios hit **0%** — refuting the maintainer-guidance hypothesis that
tv/ios are the non-gated fix for *this* content. So android does NOT under-report
here; it downloads everything. We keep `["android","web"]` rather than the
tie-break winner `["android"]` for the `web` fallback's robustness in an
unattended runner (~1.4s/video cost, both 100%). Re-measure with spike_clients.py
before changing it, only outside a 429 cooldown. See LEARNINGS.md (Phase-2 result).
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
