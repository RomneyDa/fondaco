"""Strategies for Polymarket crypto up/down prediction markets."""

from typing import Dict, Type

from bots.updown.late_sniper import LateSniper
from bots.updown.poisson_kelly import PoissonKelly
from bots.updown.late_sniper_safe import LateSniperSafe
from bots.updown.drift_sniper import DriftSniper
from bots.updown.wave_rider import WaveRider

STRATEGIES = {
    "LateSniper": LateSniper,
    "PoissonKelly": PoissonKelly,
    "LateSniperSafe": LateSniperSafe,
    "DriftSniper": DriftSniper,
    "WaveRider": WaveRider,
}  # type: Dict[str, type]
