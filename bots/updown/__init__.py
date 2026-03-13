"""Strategies for Polymarket crypto up/down prediction markets."""

from typing import Dict

from bots.updown.poisson_kelly import PoissonKelly
from bots.updown.drift_sniper import DriftSniper
from bots.updown.poisson_kelly_dynamic import PoissonKellyDynamic
from bots.updown.gap_fader import GapFader
from bots.updown.regime_sniper import RegimeSniper
from bots.updown.trend_drift import TrendDrift
from bots.updown.mean_rev_fader import MeanRevFader
from bots.updown.vol_breakout import VolBreakout
from bots.updown.confirmed_late import ConfirmedLate
from bots.updown.poisson_kelly_gated import PoissonKellyGated
from bots.updown.late_sniper_cb import LateSniperCB
from bots.updown.scalper import Scalper
from bots.updown.mary_poppins import MaryPoppins

STRATEGIES = {
    "PoissonKelly": PoissonKelly,
    "PoissonKellyDynamic": PoissonKellyDynamic,
    "DriftSniper": DriftSniper,
    "GapFader": GapFader,
    "RegimeSniper": RegimeSniper,
    "TrendDrift": TrendDrift,
    "MeanRevFader": MeanRevFader,
    "VolBreakout": VolBreakout,
    "ConfirmedLate": ConfirmedLate,
    "PoissonKellyGated": PoissonKellyGated,
    "LateSniperCB": LateSniperCB,
    "Scalper": Scalper,
    "MaryPoppins": MaryPoppins,
}  # type: Dict[str, type]
