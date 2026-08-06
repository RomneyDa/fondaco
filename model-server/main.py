"""Sports model server.

Serves fitted scoring-rate *parameters* to the Rust evaluator — never a
per-tick probability. This is the "Python fits, Rust evaluates" split:

  * At game start (and when team state changes, e.g. a red card), Rust asks
    once for `{lambda_home, lambda_away, rho}`.
  * Rust then computes the in-play win/draw/loss probability locally from
    (rates, current score, minutes remaining) in microseconds, every tick.

Calling into Python per tick would be both too slow and pointlessly chatty: the
fit only changes when new *results* arrive, which is weekly, not when the clock
moves.

Usage:
    cd model-server
    uv sync
    uv run python main.py [--port 8422]

Fit from historical CSVs first:
    uv run python main.py --fit-dir ./data/epl --league epl
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from flask import Flask, jsonify, request

from dixon_coles import DixonColesFit, fit as fit_dixon_coles, load_football_data_csv

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

PARAMS_DIR = Path(os.environ.get("FONDACO_MODEL_DIR", Path(__file__).parent / "params"))

# league key -> fit
_fits: dict[str, DixonColesFit] = {}

# League-average fallbacks, matching GameParams::league_average on the Rust side.
LEAGUE_AVERAGE = {
    "soccer": {"home_rate": 1.50, "away_rate": 1.20, "rho": -0.05},
    "american_football": {"home_rate": 24.0, "away_rate": 21.5, "rho": 0.0},
}

SOCCER_LEAGUES = {"epl", "laliga", "seriea", "bundesliga", "ucl", "mls"}


def sport_of(league: str) -> str:
    return "soccer" if league in SOCCER_LEAGUES else "american_football"


def params_path(league: str) -> Path:
    return PARAMS_DIR / f"{league}.json"


def save_fit(league: str, f: DixonColesFit) -> None:
    PARAMS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "league": league,
        "attack": f.attack,
        "defence": f.defence,
        "home_advantage": f.home_advantage,
        "rho": f.rho,
        "n_matches": f.n_matches,
        "log_likelihood": f.log_likelihood,
        "teams": f.teams,
    }
    tmp = params_path(league).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(params_path(league))
    logger.info("Wrote %s (%d matches, %d teams)", params_path(league), f.n_matches, len(f.teams))


def load_fit(league: str) -> DixonColesFit | None:
    p = params_path(league)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
        return DixonColesFit(
            attack=raw.get("attack", {}),
            defence=raw.get("defence", {}),
            home_advantage=raw.get("home_advantage", 0.0),
            rho=raw.get("rho", 0.0),
            n_matches=raw.get("n_matches", 0),
            log_likelihood=raw.get("log_likelihood", 0.0),
            teams=raw.get("teams", []),
        )
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not load %s: %s", p, e)
        return None


def get_fit(league: str) -> DixonColesFit | None:
    if league not in _fits:
        f = load_fit(league)
        if f is not None:
            _fits[league] = f
    return _fits.get(league)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "leagues_loaded": sorted(_fits.keys()),
            "params_dir": str(PARAMS_DIR),
        }
    )


@app.route("/params", methods=["POST"])
def params():
    """Return scoring rates for one fixture.

    Request:  {"league": "epl", "home": "Arsenal", "away": "Chelsea"}
    Response: {"home_rate": 1.8, "away_rate": 1.1, "rho": -0.04,
               "red_card_factor": 0.75, "fitted": true}

    `fitted` is the critical field. When false, the rates are league averages
    that know nothing about which team is better, and the Rust side must mark
    the estimate untradeable rather than treating an apparent edge against them
    as signal.
    """
    body = request.get_json(silent=True) or {}
    league = str(body.get("league", "")).lower()
    home = str(body.get("home", ""))
    away = str(body.get("away", ""))

    sport = sport_of(league)
    avg = LEAGUE_AVERAGE[sport]

    f = get_fit(league)
    if f is None or not f.knows(home, away):
        if f is not None:
            logger.info(
                "league=%s has no fitted strengths for '%s' vs '%s' — falling back",
                league,
                home,
                away,
            )
        return jsonify(
            {
                "home_rate": avg["home_rate"],
                "away_rate": avg["away_rate"],
                "rho": avg["rho"],
                "red_card_factor": 0.75,
                "fitted": False,
                "reason": "no fit available for these teams",
            }
        )

    lam, mu = f.rates(home, away)
    return jsonify(
        {
            "home_rate": lam,
            "away_rate": mu,
            "rho": f.rho,
            "red_card_factor": 0.75,
            "fitted": True,
            "n_matches": f.n_matches,
        }
    )


@app.route("/teams", methods=["GET"])
def teams():
    league = request.args.get("league", "").lower()
    f = get_fit(league)
    if f is None:
        return jsonify({"league": league, "teams": [], "fitted": False})
    return jsonify({"league": league, "teams": f.teams, "fitted": True})


@app.route("/fit", methods=["POST"])
def refit():
    """Refit a league from CSVs on disk.

    Request: {"league": "epl", "dir": "/path/to/csvs", "half_life_days": 180}
    """
    body = request.get_json(silent=True) or {}
    league = str(body.get("league", "")).lower()
    directory = body.get("dir")
    half_life = float(body.get("half_life_days", 180.0))

    if not league or not directory:
        return jsonify({"error": "league and dir are required"}), 400

    try:
        f = fit_league(league, directory, half_life)
    except (ValueError, OSError) as e:
        return jsonify({"error": str(e)}), 400

    return jsonify(
        {
            "league": league,
            "n_matches": f.n_matches,
            "n_teams": len(f.teams),
            "home_advantage": f.home_advantage,
            "rho": f.rho,
            "log_likelihood": f.log_likelihood,
        }
    )


def fit_league(league: str, directory: str, half_life_days: float = 180.0) -> DixonColesFit:
    """Fit one league from every CSV in a directory."""
    matches = []
    for path in sorted(Path(directory).glob("*.csv")):
        found = load_football_data_csv(str(path))
        logger.info("%s: %d matches", path.name, len(found))
        matches.extend(found)

    if not matches:
        raise ValueError(f"no usable matches found in {directory}")

    f = fit_dixon_coles(matches, half_life_days=half_life_days)
    _fits[league] = f
    save_fit(league, f)
    return f


def main() -> None:
    parser = argparse.ArgumentParser(description="Fondaco sports model server")
    parser.add_argument("--port", type=int, default=8422)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--fit-dir", help="Fit from CSVs in this directory, then exit")
    parser.add_argument("--league", help="League key for --fit-dir")
    parser.add_argument("--half-life-days", type=float, default=180.0)
    args = parser.parse_args()

    if args.fit_dir:
        if not args.league:
            parser.error("--fit-dir requires --league")
        f = fit_league(args.league, args.fit_dir, args.half_life_days)
        print(
            f"Fitted {args.league}: {f.n_matches} matches, {len(f.teams)} teams, "
            f"home advantage {f.home_advantage:.3f}, rho {f.rho:.3f}"
        )
        return

    # Preload whatever is already on disk.
    PARAMS_DIR.mkdir(parents=True, exist_ok=True)
    for p in PARAMS_DIR.glob("*.json"):
        league = p.stem
        if get_fit(league):
            logger.info("Loaded fit for %s", league)

    logger.info("Model server listening on %s:%d", args.host, args.port)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
