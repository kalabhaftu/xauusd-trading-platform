from app.domain import Direction
from app.indicators.core import adx, atr, bollinger, rsi
from app.strategies.base import Strategy


class MeanReversion(Strategy):
    name = "mean_reversion"
    description = "Bollinger re-entry after RSI extreme, ADX below 20; ATR stop and middle-band target >=1R."

    def signals(self, frames, symbol):
        frame = frames[self.parameters.entry_timeframe]
        if frame.empty:
            return []
        p = self.parameters
        bands = bollinger(frame.close, p.band_period, p.band_std)
        strength, momentum, volatility = adx(frame, p.adx_period), rsi(frame.close, p.rsi_period), atr(frame, p.atr_period)
        result = []
        for i in range(1, len(frame)):
            if not strength.iloc[i] < p.adx_max:
                continue
            long = frame.close.iloc[i - 1] < bands.lower.iloc[i - 1] \
                and momentum.iloc[i - 1] < p.rsi_low and frame.close.iloc[i] >= bands.lower.iloc[i]
            short = frame.close.iloc[i - 1] > bands.upper.iloc[i - 1] \
                and momentum.iloc[i - 1] > p.rsi_high and frame.close.iloc[i] <= bands.upper.iloc[i]
            if long or short:
                signal = self.make_signal(frame, i, symbol, Direction.LONG if long else Direction.SHORT,
                                          volatility.iloc[i], "Band re-entry, prior RSI extreme and weak trend",
                                          "range", {"atr": volatility.iloc[i], "rsi": momentum.iloc[i],
                                                    "adx": strength.iloc[i]}, target=float(bands.middle.iloc[i]))
                if signal:
                    result.append(signal)
        return result
