"""Standalone daily MLB training-data capture (Phase 1).

Runs the two capture steps -- outcome resolution for prior dates, then pre-game
feature capture for today's slate -- with NO dependency on the stats/insights/
Pages build. That separation is the point: .github/workflows/capture-training-data.yml
runs this on its own schedule with its own `contents: write` permission, so
deploy-pages.yml keeps `contents: read` and a capture failure can never affect
the site deploy.

The same two steps also run inside the normal pipeline (generate_insights.run),
so a local run captures too. Both paths are safe to run on the same day:
capture_features() is skip-if-present on (gamePk, date), so the earliest
pre-game snapshot wins and any later run is a no-op.

Deliberately read-only outside data/training/: the boxscore cache is loaded to
serve bullpen-ERA lookups but its pruned copy is discarded rather than saved, so
this entrypoint's only writes are the two append-only training stores.

Exit status is 0 unless BOTH steps fail -- a partial capture is a normal day
(e.g. nothing pending to resolve), not a workflow failure.
"""

import datetime
import json
import os
import sys

import yaml

import training_capture
from fetchers import mlb
from generate_insights import BOXSCORE_CACHE_PATH, schedule_fetcher

CONFIG_PATH = "config.yaml"


def _load_boxscore_cache():
    """The committed boxscore cache, read-only. Feeds bullpen-ERA (7d) lookups;
    the pruned copy build_game_entities returns is intentionally NOT written
    back here -- see the module docstring."""
    if not os.path.exists(BOXSCORE_CACHE_PATH):
        return {}
    try:
        with open(BOXSCORE_CACHE_PATH) as f:
            return json.load(f)
    except (ValueError, OSError):
        print("capture: boxscore cache unreadable; continuing without it")
        return {}


def main():
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date()
    print("capture: MLB training data for {} (run at {})"
          .format(today.isoformat(), training_capture._iso(now)))

    ok = 0

    # 1. Outcomes first -- label completed prior games before today's slate is
    #    touched, so labels are always resolved from a finished game's own record.
    try:
        training_capture.resolve_outcomes(schedule_fetcher(config), today)
        ok += 1
    except Exception as e:  # noqa: BLE001
        print("capture: outcome resolution failed ({})".format(str(e)[:200]))

    # 2. Pre-game features for today's slate. build_game_entities applies the
    #    leakage gates per game via training_capture.build_feature_row.
    try:
        entities, _pruned_cache, training_rows = mlb.build_game_entities(
            config, today.isoformat(), _load_boxscore_cache())
        written, skipped = training_capture.capture_features(training_rows)
        print("capture: {} games on slate, {} rows passed the pre-game gates, "
              "{} written, {} already on file"
              .format(len(entities), len(training_rows), written, skipped))
        ok += 1
    except Exception as e:  # noqa: BLE001
        print("capture: feature capture failed ({})".format(str(e)[:200]))

    if ok == 0:
        print("capture: both steps failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
