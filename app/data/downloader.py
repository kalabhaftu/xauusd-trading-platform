from datetime import datetime, timedelta

import pandas as pd

from app.data.validation import TIMEFRAMES, closed, validate
from app.domain import digest, utcnow


def download(broker, store, symbol: str, timeframe: str, start: datetime, end: datetime, refresh=False) -> dict:
    if timeframe not in TIMEFRAMES or start >= end:
        raise ValueError("Invalid timeframe or historical range")
    account = broker.account()
    source = account.server
    spec = broker.spec(symbol)
    store.set(f"spec:{source}:{symbol}", spec)
    cursor = start
    empty_chunks = []
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=7 if timeframe == "M1" else 30), end)
        chunk_key = "download_chunk:" + digest([source, symbol, timeframe, cursor.isoformat(), chunk_end.isoformat()])
        existing = store.load_candles(source, symbol, timeframe, cursor.isoformat(), chunk_end.isoformat())
        # Fully covered chunks need no repeat transfer. Gaps are retried, never fabricated.
        expected_count = int((chunk_end - cursor).total_seconds() / (TIMEFRAMES[timeframe] * 60))
        if refresh or (len(existing) < expected_count and not store.get(chunk_key)):
            frame = broker.candles(symbol, timeframe,
                                   cursor - timedelta(minutes=TIMEFRAMES[timeframe]), chunk_end)
            if frame.empty:
                empty_chunks.append([cursor.isoformat(), chunk_end.isoformat()])
            else:
                validate(frame, timeframe)
                frame = closed(frame, timeframe, min(end, utcnow()))
                frame = frame.loc[(frame.index >= start) & (frame.index < end)]
                store.save_candles(frame, source, symbol, timeframe, spec.price_basis)
                store.set(chunk_key, {"rows_received": len(frame), "timestamp": utcnow().isoformat()})
        cursor = chunk_end
    result = store.load_candles(source, symbol, timeframe, start.isoformat(), end.isoformat())
    quality = validate(result, timeframe)
    quality.update(source=source, symbol=symbol, timeframe=timeframe, requested_start=start.isoformat(),
                   requested_end=end.isoformat(), empty_chunks=empty_chunks, price_basis=spec.price_basis,
                   limitations="OHLC and candle spread cannot establish intrabar bid/ask execution")
    if not result.empty:
        quality["leading_history_shortfall_seconds"] = max(0, (result.index[0].to_pydatetime() - start).total_seconds())
        quality["trailing_history_shortfall_seconds"] = max(0, (
            end - result.index[-1].to_pydatetime() - timedelta(minutes=TIMEFRAMES[timeframe])).total_seconds())
    store.record("data_quality", quality)
    return quality


def research_frames(store, source: str, symbol: str, entry="M15", trend=None) -> dict[str, pd.DataFrame]:
    # Strategy implementations may request additional timeframes in a future
    # version; current researched rules are single-timeframe and use completed
    # entry bars only.
    timeframes = [entry] if trend is None or trend == entry else [entry, trend]
    return {tf: store.load_candles(source, symbol, tf) for tf in timeframes}
