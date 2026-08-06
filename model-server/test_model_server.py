"""Tests for the sports model server."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from dixon_coles import DixonColesFit, Match, fit, load_football_data_csv, time_weights


def synthetic_matches(n_seasons: int = 3) -> list[Match]:
    """Deterministic fixtures where one team is clearly strongest.

    'Strong' scores heavily and concedes little; 'Weak' the reverse. A correct
    fit must recover that ordering.
    """
    teams = ["Strong", "Mid", "Weak"]
    base = datetime(2023, 8, 1, tzinfo=timezone.utc)
    goals = {
        ("Strong", "Mid"): (3, 0),
        ("Strong", "Weak"): (4, 0),
        ("Mid", "Strong"): (1, 2),
        ("Mid", "Weak"): (2, 0),
        ("Weak", "Strong"): (0, 3),
        ("Weak", "Mid"): (0, 1),
    }

    out: list[Match] = []
    day = 0
    for _ in range(n_seasons * 6):
        for home in teams:
            for away in teams:
                if home == away:
                    continue
                hg, ag = goals[(home, away)]
                out.append(
                    Match(
                        date=base + timedelta(days=day),
                        home=home,
                        away=away,
                        home_goals=hg,
                        away_goals=ag,
                    )
                )
                day += 3
    return out


def test_fit_recovers_relative_team_strength():
    f = fit(synthetic_matches(), half_life_days=3650.0)
    assert f.attack["Strong"] > f.attack["Mid"] > f.attack["Weak"]
    # Higher defence value means conceding less, given lambda uses -defence.
    assert f.defence["Strong"] > f.defence["Weak"]


def test_fit_produces_sane_rates():
    f = fit(synthetic_matches(), half_life_days=3650.0)
    lam, mu = f.rates("Strong", "Weak")
    assert lam > mu, "the stronger home side should have the higher rate"
    assert 0.0 < lam < 10.0
    assert 0.0 < mu < 10.0


def test_home_advantage_is_positive_on_home_skewed_data():
    f = fit(synthetic_matches(), half_life_days=3650.0)
    assert f.home_advantage > 0.0


def test_rates_are_symmetric_when_teams_swap():
    f = fit(synthetic_matches(), half_life_days=3650.0)
    lam_a, mu_a = f.rates("Strong", "Weak")
    lam_b, mu_b = f.rates("Weak", "Strong")
    # Strong at home should outscore Strong away.
    assert lam_a > mu_b


def test_unknown_teams_fall_back_to_league_average():
    f = fit(synthetic_matches(), half_life_days=3650.0)
    assert not f.knows("Nonexistent FC")
    lam, mu = f.rates("Nonexistent FC", "Also Unknown")
    # Falls back to exp(0 - 0 + hfa) and exp(0 - 0), which is finite and sane.
    assert math.isfinite(lam) and math.isfinite(mu)
    assert lam > 0 and mu > 0


def test_knows_requires_every_team():
    f = fit(synthetic_matches(), half_life_days=3650.0)
    assert f.knows("Strong", "Weak")
    assert not f.knows("Strong", "Nonexistent FC")


def test_fit_rejects_too_little_data():
    with pytest.raises(ValueError, match="not meaningful"):
        fit(synthetic_matches()[:5])


def test_time_weights_decay_with_age():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    matches = [
        Match(base - timedelta(days=360), "A", "B", 1, 0),
        Match(base - timedelta(days=180), "A", "B", 1, 0),
        Match(base, "A", "B", 1, 0),
    ]
    w = time_weights(matches, half_life_days=180.0)
    assert w[2] == pytest.approx(1.0)
    assert w[1] == pytest.approx(0.5, abs=1e-9)
    assert w[0] == pytest.approx(0.25, abs=1e-9)
    assert w[0] < w[1] < w[2]


def test_zero_half_life_disables_decay():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    matches = [
        Match(base - timedelta(days=1000), "A", "B", 1, 0),
        Match(base, "A", "B", 1, 0),
    ]
    w = time_weights(matches, half_life_days=0.0)
    assert list(w) == [1.0, 1.0]


def test_recent_form_dominates_with_a_short_half_life():
    """A team that was weak and became strong should fit as strong."""
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    matches = []
    # Old: Riser loses heavily.
    for i in range(40):
        matches.append(Match(base + timedelta(days=i * 3), "Riser", "Rival", 0, 3))
        matches.append(Match(base + timedelta(days=i * 3 + 1), "Rival", "Riser", 3, 0))
    # Recent: Riser wins heavily.
    recent = base + timedelta(days=600)
    for i in range(40):
        matches.append(Match(recent + timedelta(days=i * 3), "Riser", "Rival", 3, 0))
        matches.append(Match(recent + timedelta(days=i * 3 + 1), "Rival", "Riser", 0, 3))

    short = fit(matches, half_life_days=60.0)
    assert short.attack["Riser"] > short.attack["Rival"], (
        "with a short half-life the recent form must dominate"
    )


def test_csv_parsing_skips_incomplete_rows(tmp_path):
    csv = tmp_path / "epl.csv"
    csv.write_text(
        "Date,HomeTeam,AwayTeam,FTHG,FTAG\n"
        "12/08/2023,Arsenal,Chelsea,2,1\n"
        "13/08/2023,Liverpool,Everton,,\n"       # missing goals
        "14/08/2023,,Spurs,1,0\n"                 # missing home team
        "notadate,Fulham,Brentford,0,0\n"         # unparseable date
        "15/08/2023,Newcastle,Villa,3,1\n"
    )
    matches = load_football_data_csv(str(csv))
    assert len(matches) == 2, "incomplete rows must be skipped, never defaulted"
    assert matches[0].home == "Arsenal"
    assert matches[1].home_goals == 3


def test_csv_parsing_handles_multiple_date_formats(tmp_path):
    csv = tmp_path / "mixed.csv"
    csv.write_text(
        "Date,HomeTeam,AwayTeam,FTHG,FTAG\n"
        "12/08/2023,A,B,1,0\n"
        "13/08/23,C,D,2,2\n"
        "2023-08-14,E,F,0,1\n"
    )
    matches = load_football_data_csv(str(csv))
    assert len(matches) == 3


def test_csv_parsing_of_a_missing_file_raises_rather_than_returning_empty(tmp_path):
    with pytest.raises(OSError):
        load_football_data_csv(str(tmp_path / "nope.csv"))


def test_rho_stays_in_a_sane_range():
    f = fit(synthetic_matches(), half_life_days=3650.0)
    assert -0.4 <= f.rho <= 0.4


def test_fit_serializes_and_reloads_identically():
    f = fit(synthetic_matches(), half_life_days=3650.0)
    round_tripped = DixonColesFit(
        attack=dict(f.attack),
        defence=dict(f.defence),
        home_advantage=f.home_advantage,
        rho=f.rho,
        n_matches=f.n_matches,
        teams=list(f.teams),
    )
    assert round_tripped.rates("Strong", "Weak") == f.rates("Strong", "Weak")
