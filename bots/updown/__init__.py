"""Strategies for Polymarket crypto up/down prediction markets."""

from typing import Dict, Type

from bots.updown.momentum_entry import MomentumEntry
from bots.updown.midpoint_momentum import MidpointMomentum
from bots.updown.late_sniper import LateSniper
from bots.updown.fade_the_market import FadeTheMarket
from bots.updown.buy_trend import BuyTrend
from bots.updown.digital_option import DigitalOption
from bots.updown.cheap_shot import CheapShot
from bots.updown.brenda_flipper import BrendaFlipper
from bots.updown.bargain_hunter import BargainHunter
from bots.updown.dip_sniper import DipSniper
from bots.updown.poisson_kelly import PoissonKelly
from bots.updown.poisson_kelly_active import PoissonKellyActive
from bots.updown.poisson_kelly_historical import PoissonKellyHistorical
from bots.updown.dynamic_poisson_kelly import DynamicPoissonKelly
from bots.updown.poisson_kelly_stacked import PoissonKellyStacked
from bots.updown.smart_late_sniper import SmartLateSniper
from bots.updown.late_sniper_safe import LateSniperSafe

STRATEGIES = {
    "MomentumEntry": MomentumEntry,
    "MidpointMomentum": MidpointMomentum,
    "LateSniper": LateSniper,
    "FadeTheMarket": FadeTheMarket,
    "BuyTrend": BuyTrend,
    "DigitalOption": DigitalOption,
    "CheapShot": CheapShot,
    "BrendaFlipper": BrendaFlipper,
    "BargainHunter": BargainHunter,
    "DipSniper": DipSniper,
    "PoissonKelly": PoissonKelly,
    "PoissonKellyActive": PoissonKellyActive,
    "PoissonKellyHistorical": PoissonKellyHistorical,
    "DynamicPoissonKelly": DynamicPoissonKelly,
    "PoissonKellyStacked": PoissonKellyStacked,
    "SmartLateSniper": SmartLateSniper,
    "LateSniperSafe": LateSniperSafe,
}  # type: Dict[str, type]
