"""Strategies for Polymarket crypto up/down prediction markets."""

from typing import Dict

from bots.updown.late_sniper import LateSniper
from bots.updown.poisson_kelly import PoissonKelly
from bots.updown.late_sniper_safe import LateSniperSafe
from bots.updown.drift_sniper import DriftSniper
from bots.updown.wave_rider import WaveRider
from bots.updown.poisson_kelly_dynamic import PoissonKellyDynamic
from bots.updown.gap_fader import GapFader

STRATEGIES = {
    "LateSniper": LateSniper,
    "PoissonKelly": PoissonKelly,
    "PoissonKellyDynamic": PoissonKellyDynamic,
    "LateSniperSafe": LateSniperSafe,
    "DriftSniper": DriftSniper,
    "WaveRider": WaveRider,
    "GapFader": GapFader,
}  # type: Dict[str, type]
