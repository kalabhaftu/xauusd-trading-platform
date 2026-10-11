from pydantic import Field

from app.domain import Model


class ResearchPolicy(Model):
    min_profit_factor: float = Field(default=1.3, gt=1)
    max_drawdown: float = Field(default=.10, gt=0, lt=1)
    min_oos_trades: int = Field(default=200, ge=1)
    min_training_trades: int = Field(default=20, ge=1)
    min_forward_folds: int = Field(default=3, ge=1)
    positive_fold_fraction: float = Field(default=.6, gt=0, le=1)
    max_single_win_share: float = Field(default=.2, gt=0, le=1)
    training_months: int = Field(default=12, ge=1)
    forward_months: int = Field(default=3, ge=1)
