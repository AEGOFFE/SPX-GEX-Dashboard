"""
Black-Scholes greeks for SPX (European, cash-settled, q=0).
Vectorized on (S, T) so we can build heatmap grids fast.

Conventions:
- S: underlying price (scalar or ndarray)
- K: strike (scalar or ndarray, must broadcast with S)
- T: time to expiry in YEARS (scalar or ndarray)
- sigma: implied vol as a decimal (e.g. 0.15 for 15%)
- r: risk-free rate as a decimal
- q: dividend yield (SPX index: use 0 or ~0.013; 0 is fine for 0DTE)

Gamma units: per $1 move in S.
Charm units: delta change per YEAR. Divide by 252 for "per trading day"
or by (252*390) for "per minute" if you want human-scale numbers.
"""
import numpy as np
from math import erf, sqrt, pi, log, exp

SQRT_2PI = sqrt(2.0 * pi)

def _norm_pdf(x):
    return np.exp(-0.5 * x * x) / SQRT_2PI

def _norm_cdf(x):
    # vectorized erf via numpy
    return 0.5 * (1.0 + np.vectorize(erf)(x / sqrt(2.0)))

def _d1_d2(S, K, T, sigma, r, q=0.0):
    # Guard against T=0 / sigma=0 — clip tiny values
    T = np.maximum(T, 1e-8)
    sigma = np.maximum(sigma, 1e-4)
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return d1, d2

def bs_gamma(S, K, T, sigma, r=0.04, q=0.0):
    """Gamma is the same for calls and puts."""
    d1, _ = _d1_d2(S, K, T, sigma, r, q)
    return _norm_pdf(d1) / (S * sigma * np.sqrt(np.maximum(T, 1e-8)))

def bs_charm(S, K, T, sigma, r=0.04, q=0.0, option_type='call'):
    """
    Charm = dDelta/dt. Returns change in delta per year (negative sign = delta
    decreases as time passes for an OTM call that's bleeding to 0).
    """
    T = np.maximum(T, 1e-8)
    sigma = np.maximum(sigma, 1e-4)
    d1, d2 = _d1_d2(S, K, T, sigma, r, q)
    pdf = _norm_pdf(d1)

    # Standard closed form
    term = (2 * (r - q) * T - d2 * sigma * np.sqrt(T)) / (2 * T * sigma * np.sqrt(T))
    call_charm = -q * np.exp(-q * T) * _norm_cdf(d1) - np.exp(-q * T) * pdf * term

    if option_type == 'call':
        return call_charm
    else:
        # put charm via parity
        return call_charm + q * np.exp(-q * T)
