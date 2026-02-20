"""Trading strategies for Polymarket up/down markets."""

from core.strategies.base import Strategy
from core.strategies.always_buy_up import AlwaysBuyUp
from core.strategies.always_buy_down import AlwaysBuyDown
from core.strategies.momentum_entry import MomentumEntry
from core.strategies.midpoint_momentum import MidpointMomentum
from core.strategies.late_sniper import LateSniper
from core.strategies.fade_the_market import FadeTheMarket
from core.strategies.ema_crossover import EmaCrossover
from core.strategies.ols_slope import OlsSlope
from core.strategies.scalper import Scalper
from core.strategies.trailing_stop_momentum import TrailingStopMomentum

ALL_STRATEGIES = {
    "AlwaysBuyUp": AlwaysBuyUp,
    "AlwaysBuyDown": AlwaysBuyDown,
    "MomentumEntry": MomentumEntry,
    "MidpointMomentum": MidpointMomentum,
    "LateSniper": LateSniper,
    "FadeTheMarket": FadeTheMarket,
    "EmaCrossover": EmaCrossover,
    "OlsSlope": OlsSlope,
    "Scalper": Scalper,
    "TrailingStopMomentum": TrailingStopMomentum,
}
