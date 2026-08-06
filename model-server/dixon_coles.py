"""Dixon-Coles team-strength fitting for soccer.

This is the *offline* half of the "Python fits, Rust evaluates" split. It
estimates per-team attack and defence strengths plus a home-advantage term and
the low-score correlation parameter rho, then hands those to the Rust
evaluator, which computes in-play probabilities locally on every tick.

The split matters: an HTTP round trip per tick would be both too slow and
pointlessly chatty, and the fit only changes when new results arrive (weekly),
not when the clock moves.

Model
-----
Goals are Poisson with

    lambda_home = exp(attack[home] - defence[away] + home_advantage)
    lambda_away = exp(attack[away] - defence[home])

plus the Dixon-Coles tau correction on the four low-score cells (0-0, 0-1, 1-0,
1-1), where independent Poisson is known to misprice, and an exponential
time-decay weight so recent matches count more.

Data
----
Free historical results with closing odds are available from football-data.co.uk
as per-league, per-season CSVs. Only four columns are needed: date, home team,
away team, and full-time goals for each side.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
from scipy.optimize import minimize


@dataclass
class Match:
    date: datetime
    home: str
    away: str
    home_goals: int
    away_goals: int


@dataclass
class DixonColesFit:
    attack: dict[str, float] = field(default_factory=dict)
    defence: dict[str, float] = field(default_factory=dict)
    home_advantage: float = 0.0
    rho: float = 0.0
    n_matches: int = 0
    log_likelihood: float = 0.0
    teams: list[str] = field(default_factory=list)

    def rates(self, home: str, away: str) -> tuple[float, float]:
        """Expected full-match goals for (home, away).

        Unknown teams fall back to league-average strength (0 in log space).
        The caller must treat a fit involving an unknown team as untrusted —
        the Rust side marks such params ``ModelFallback`` and refuses to size a
        position from them.
        """
        ah = self.attack.get(home, 0.0)
        dh = self.defence.get(home, 0.0)
        aa = self.attack.get(away, 0.0)
        da = self.defence.get(away, 0.0)
        lam = math.exp(ah - da + self.home_advantage)
        mu = math.exp(aa - dh)
        return lam, mu

    def knows(self, *teams: str) -> bool:
        return all(t in self.attack for t in teams)


def _tau(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    """Dixon-Coles correction for the four low-scoring cells."""
    if x == 0 and y == 0:
        return 1.0 - lam * mu * rho
    if x == 0 and y == 1:
        return 1.0 + lam * rho
    if x == 1 and y == 0:
        return 1.0 + mu * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def _poisson_logpmf(k: np.ndarray, lam: np.ndarray) -> np.ndarray:
    from scipy.special import gammaln

    return k * np.log(np.maximum(lam, 1e-12)) - lam - gammaln(k + 1.0)


def time_weights(matches: list[Match], half_life_days: float) -> np.ndarray:
    """Exponential decay so recent form dominates.

    Without this the fit weights a match from four seasons ago as heavily as
    last week's, which is plainly wrong for team strength.
    """
    if not matches:
        return np.array([])
    latest = max(m.date for m in matches)
    ages = np.array([(latest - m.date).days for m in matches], dtype=float)
    if half_life_days <= 0:
        return np.ones_like(ages)
    return 0.5 ** (ages / half_life_days)


def fit(
    matches: list[Match],
    half_life_days: float = 180.0,
    max_iter: int = 300,
) -> DixonColesFit:
    """Fit attack/defence strengths, home advantage and rho by weighted MLE."""
    if len(matches) < 20:
        raise ValueError(
            f"only {len(matches)} matches supplied; a Dixon-Coles fit on this "
            "little data is not meaningful"
        )

    teams = sorted({m.home for m in matches} | {m.away for m in matches})
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)

    home_i = np.array([idx[m.home] for m in matches])
    away_i = np.array([idx[m.away] for m in matches])
    hg = np.array([m.home_goals for m in matches], dtype=float)
    ag = np.array([m.away_goals for m in matches], dtype=float)
    w = time_weights(matches, half_life_days)

    # Parameter vector: [attack (n), defence (n), home_advantage, rho].
    # Attack is sum-to-zero constrained via a penalty, which keeps the
    # optimisation unconstrained and well conditioned.
    def unpack(p):
        return p[:n], p[n : 2 * n], p[2 * n], p[2 * n + 1]

    def neg_log_likelihood(p):
        attack, defence, hfa, rho = unpack(p)
        rho = float(np.clip(rho, -0.99, 0.99))

        lam = np.exp(attack[home_i] - defence[away_i] + hfa)
        mu = np.exp(attack[away_i] - defence[home_i])
        lam = np.clip(lam, 1e-6, 25.0)
        mu = np.clip(mu, 1e-6, 25.0)

        ll = _poisson_logpmf(hg, lam) + _poisson_logpmf(ag, mu)

        # Dixon-Coles correction, applied only to the low-score cells.
        tau = np.ones_like(ll)
        m00 = (hg == 0) & (ag == 0)
        m01 = (hg == 0) & (ag == 1)
        m10 = (hg == 1) & (ag == 0)
        m11 = (hg == 1) & (ag == 1)
        tau[m00] = 1.0 - lam[m00] * mu[m00] * rho
        tau[m01] = 1.0 + lam[m01] * rho
        tau[m10] = 1.0 + mu[m10] * rho
        tau[m11] = 1.0 - rho
        # A large rho can drive a cell non-positive; guard the log.
        tau = np.maximum(tau, 1e-9)

        ll = ll + np.log(tau)
        total = float(np.sum(w * ll))

        # Identifiability: attack and defence are only defined up to a shared
        # constant, so pin their means.
        penalty = 1000.0 * (attack.mean() ** 2 + defence.mean() ** 2)
        return -total + penalty

    x0 = np.concatenate([np.zeros(n), np.zeros(n), [0.25], [-0.05]])
    result = minimize(
        neg_log_likelihood,
        x0,
        method="L-BFGS-B",
        bounds=[(-3, 3)] * (2 * n) + [(-1, 2), (-0.4, 0.4)],
        options={"maxiter": max_iter},
    )

    attack, defence, hfa, rho = unpack(result.x)
    return DixonColesFit(
        attack={t: float(attack[idx[t]]) for t in teams},
        defence={t: float(defence[idx[t]]) for t in teams},
        home_advantage=float(hfa),
        rho=float(np.clip(rho, -0.99, 0.99)),
        n_matches=len(matches),
        log_likelihood=float(-result.fun),
        teams=teams,
    )


def load_football_data_csv(path: str) -> list[Match]:
    """Parse a football-data.co.uk CSV.

    Columns used: Date, HomeTeam, AwayTeam, FTHG, FTAG. Rows missing any of
    them are skipped rather than defaulted — a match silently read as 0-0 would
    corrupt every team's strength.
    """
    import csv

    out: list[Match] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                raw_date = (row.get("Date") or "").strip()
                date = None
                for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
                    try:
                        date = datetime.strptime(raw_date, fmt).replace(
                            tzinfo=timezone.utc
                        )
                        break
                    except ValueError:
                        continue
                if date is None:
                    continue

                home = (row.get("HomeTeam") or "").strip()
                away = (row.get("AwayTeam") or "").strip()
                hg = row.get("FTHG")
                ag = row.get("FTAG")
                if not home or not away or hg in (None, "") or ag in (None, ""):
                    continue

                out.append(
                    Match(
                        date=date,
                        home=home,
                        away=away,
                        home_goals=int(float(hg)),
                        away_goals=int(float(ag)),
                    )
                )
            except (ValueError, TypeError):
                continue
    return out
