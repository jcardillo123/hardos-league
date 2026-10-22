"""League history dashboard - past seasons, weekly results, manager analysis.

A separate app from the draft tool on purpose. `app/` is a draft-day instrument:
its database is wiped and rebuilt on every ingest and holds the live pick log, so
league history stored there would be one rebuild away from being collateral
damage. This package has its own database and its own server, and shares only
the ESPN credentials in `.env`.
"""

__all__ = ["config", "db", "espn", "ingest", "stats"]
