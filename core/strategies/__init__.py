"""Trading strategies for Polymarket up/down markets."""

from core.strategies.base import Strategy
from core.strategies.always_buy_up import AlwaysBuyUp
from core.strategies.always_buy_down import AlwaysBuyDown
from core.strategies.momentum_entry import MomentumEntry
from core.strategies.midpoint_momentum import MidpointMomentum
from core.strategies.late_sniper import LateSniper
from core.strategies.late_sniper_tp import LateSniperTP
from core.strategies.fade_the_market import FadeTheMarket
from core.strategies.bargain_sniper import BargainSniper
from core.strategies.buy_trend import BuyTrend
from core.strategies.digital_option import DigitalOption

ALL_STRATEGIES = {
    "AlwaysBuyUp": AlwaysBuyUp,
    "AlwaysBuyDown": AlwaysBuyDown,
    "MomentumEntry": MomentumEntry,
    "MidpointMomentum": MidpointMomentum,
    "LateSniper": LateSniper,
    "LateSniperTP": LateSniperTP,
    "FadeTheMarket": FadeTheMarket,
    "BargainSniper": BargainSniper,
    "BuyTrend": BuyTrend,
    "DigitalOption": DigitalOption,
}
