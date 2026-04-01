import pandas as pd
import numpy as np

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def bollinger_bands(close, period=20, num_std=2):
    """Bollinger Bands (布林带)"""
    middle = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = middle + std * num_std
    lower = middle - std * num_std
    return upper.fillna(0), middle.fillna(0), lower.fillna(0)

def rsi(close, period=14):
    """Relative Strength Index (RSI)"""
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0).abs()
    ma_up = up.ewm(alpha=1/period, adjust=False).mean()
    ma_down = down.ewm(alpha=1/period, adjust=False).mean()
    rs = ma_up / ma_down.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50)

def macd(close, fast=12, slow=26, signal=9):
    """MACD 指标"""
    fast_ema = ema(close, fast)
    slow_ema = ema(close, slow)
    line = fast_ema - slow_ema
    sig = ema(line, signal)
    hist = line - sig
    return line, sig, hist

def mfi(high, low, close, volume, period=14):
    """Money Flow Index (MFI) - 资金流量指标"""
    typical_price = (high + low + close) / 3
    money_flow = typical_price * volume
    
    positive_flow = []
    negative_flow = []
    
    # 获取 numpy 数组加速计算
    tp_values = typical_price.values
    mf_values = money_flow.values
    
    for i in range(len(tp_values)):
        if i == 0:
            positive_flow.append(0)
            negative_flow.append(0)
        elif tp_values[i] > tp_values[i-1]:
            positive_flow.append(mf_values[i])
            negative_flow.append(0)
        elif tp_values[i] < tp_values[i-1]:
            positive_flow.append(0)
            negative_flow.append(mf_values[i])
        else:
            positive_flow.append(0)
            negative_flow.append(0)
            
    pos_ser = pd.Series(positive_flow, index=close.index).rolling(period).sum()
    neg_ser = pd.Series(negative_flow, index=close.index).rolling(period).sum()
    
    mfr = pos_ser / neg_ser.replace(0, np.nan)
    mfi_value = 100 - 100 / (1 + mfr)
    return mfi_value.fillna(50)

def kdj(high, low, close, period=9, k_smooth=3, d_smooth=3):
    """KDJ 随机指标"""
    lowest_low = low.rolling(period).min()
    highest_high = high.rolling(period).max()
    denom = (highest_high - lowest_low).replace(0, np.nan)
    rsv = ((close - lowest_low) / denom * 100).fillna(50)
    
    # 使用平滑权重
    k = rsv.ewm(alpha=1/k_smooth, adjust=False).mean()
    d = k.ewm(alpha=1/d_smooth, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j

def add_all_indicators(df):
    """一键为 DataFrame 添加所有技术指标"""
    out = df.copy()
    close = out['close']
    high = out['high']
    low = out['low']
    
    # 基础指标
    out['rsi_14'] = rsi(close, 14)
    u, m, l = bollinger_bands(close)
    out['bb_upper'], out['bb_middle'], out['bb_lower'] = u, m, l
    
    line, sig, hist = macd(close)
    out['macd'], out['macd_signal'], out['macd_hist'] = line, sig, hist
    
    # KDJ
    k, d, j = kdj(high, low, close)
    out['kdj_k'], out['kdj_d'], out['kdj_j'] = k, d, j
    
    # 资金流 (如果有成交量)
    if 'volume' in out.columns:
        out['mfi_14'] = mfi(high, low, close, out['volume'])
    
    return out
