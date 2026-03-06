"""Strategies for Polymarket crypto up/down prediction markets."""

from typing import Dict, Type

from bots.updown.momentum_entry import MomentumEntry
from bots.updown.midpoint_momentum import MidpointMomentum
from bots.updown.late_sniper import LateSniper
from bots.updown.late_sniper_tp import LateSniperTP
from bots.updown.fade_the_market import FadeTheMarket
from bots.updown.buy_trend import BuyTrend
from bots.updown.digital_option import DigitalOption
from bots.updown.cheap_shot import CheapShot
from bots.updown.brenda_flipper import BrendaFlipper
from bots.updown.volatility_bargain import VolatilityBargain
from bots.updown.bargain_hunter import BargainHunter
from bots.updown.god_strategy import GodStrategy
from bots.updown.dip_sniper import DipSniper

STRATEGIES = {
    "MomentumEntry": MomentumEntry,
    "MidpointMomentum": MidpointMomentum,
    "LateSniper": LateSniper,
    "LateSniperTP": LateSniperTP,
    "FadeTheMarket": FadeTheMarket,
    "BuyTrend": BuyTrend,
    "DigitalOption": DigitalOption,
    "CheapShot": CheapShot,
    "BrendaFlipper": BrendaFlipper,
    "VolatilityBargain": VolatilityBargain,
    "BargainHunter": BargainHunter,
    "GodStrategy": GodStrategy,
    "DipSniper": DipSniper,
}  # type: Dict[str, type]
