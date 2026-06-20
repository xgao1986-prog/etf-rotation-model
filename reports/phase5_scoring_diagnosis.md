# Phase 5.3 Corrected Scoring Diagnosis Report

**Generated**: 2026-06-20 16:54:15

## Executive Summary

**Recommendation**: `no_momentum enters final validation`

**Reasoning**: no_momentum is not degraded in training and shows higher average validation return (13.41% vs B0.1 10.48%). It merits a final validation review.

## 1. Root Cause of vol_score = 0

### 1.1 How volatility_20 is computed

```python
df['volatility_20'] = df['close'].pct_change().rolling(20).std().shift(1) * np.sqrt(252)
```

This formula produces **annualized** volatility.  The thresholds in
`calculate_scores`, however, are written as if the number were a raw daily
volatility or a small decimal:

- `vol_score = 10` when `volatility_20 ∈ [0.01, 0.04]`
- `vol_score =  5` when `volatility_20 ∈ (0.04, 0.06]`

### 1.2 Empirical distribution

| Statistic | Value |
|-----------|-------|
| Count     | 25,394 |
| Mean      | 0.2438 |
| Median    | 0.2263 |
| Std Dev   | 0.1303 |
| Min       | 0.0043 |
| Max       | 0.8420 |
| 1%        | 0.0094 |
| 5%        | 0.0211 |
| 25%       | 0.1617 |
| 75%       | 0.3119 |
| 95%       | 0.4825 |
| 99%       | 0.6580 |

| Range | % of observations |
|-------|-------------------|
| [0.01, 0.04]   | 5.1587% |
| (0.04, 0.06]   | 0.3899% |
| > 0.06         | 93.2661% |

**Conclusion**: The thresholds are orders of magnitude too small for
annualized volatility.  This is a **design failure** (scale mismatch),
not a code bug.  `vol_score` is effectively always 0.

## 2. Factor Predictive Power (Daily Cross-Sectional Rank IC)

Only days with **≥2 non-zero factor scores** are included.

### 2.1 Overall Rank IC Summary

| Factor | H5 IC_mean | H5 IC_std | H5 IR | H10 IC_mean | H10 IC_std | H10 IR | H20 IC_mean | H20 IC_std | H20 IR |
|--------|------------|-----------|-------|-------------|------------|--------|-------------|------------|--------|
| trend | 0.0079 | 0.4459 | 0.0176 | 0.0085 | 0.4418 | 0.0193 | 0.0136 | 0.4501 | 0.0303 |
| confirm | -0.0244 | 0.5191 | -0.0470 | -0.0207 | 0.5060 | -0.0409 | -0.0244 | 0.5122 | -0.0477 |
| momentum | 0.0112 | 0.4083 | 0.0274 | 0.0321 | 0.3908 | 0.0822 | 0.0260 | 0.3753 | 0.0693 |
| volume | -0.0097 | 0.3119 | -0.0311 | -0.0058 | 0.3101 | -0.0185 | -0.0013 | 0.3005 | -0.0044 |
| volatility | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |

### 2.2 Annual Breakdown

#### trend

| Year | H5_mean | H5_std | H10_mean | H10_std | H20_mean | H20_std |
|------|---------|--------|----------|---------|----------|---------|
| 2019 | 0.0311 | 0.5347 | -0.0241 | 0.5027 | -0.0208 | 0.5218 |
| 2020 | -0.0434 | 0.4160 | -0.0275 | 0.4305 | 0.0109 | 0.4636 |
| 2021 | 0.0190 | 0.4422 | -0.0122 | 0.4410 | -0.0041 | 0.4343 |
| 2022 | -0.0333 | 0.5312 | 0.0031 | 0.5215 | -0.0127 | 0.5190 |
| 2023 | 0.0119 | 0.4465 | -0.0185 | 0.4490 | -0.0425 | 0.4326 |
| 2024 | 0.0281 | 0.4342 | 0.0176 | 0.4241 | 0.0275 | 0.4518 |
| 2025 | 0.0034 | 0.3703 | 0.0240 | 0.3622 | 0.0713 | 0.3697 |
| 2026 | 0.0934 | 0.3882 | 0.1834 | 0.3720 | 0.1299 | 0.3987 |

#### confirm

| Year | H5_mean | H5_std | H10_mean | H10_std | H20_mean | H20_std |
|------|---------|--------|----------|---------|----------|---------|
| 2019 | -0.0324 | 0.6031 | -0.1174 | 0.5974 | -0.1268 | 0.5851 |
| 2020 | -0.0778 | 0.5167 | -0.0677 | 0.5041 | -0.0152 | 0.5233 |
| 2021 | -0.0945 | 0.5240 | -0.0357 | 0.4955 | -0.1041 | 0.4866 |
| 2022 | -0.0143 | 0.5380 | -0.0473 | 0.5365 | -0.0937 | 0.5563 |
| 2023 | 0.0013 | 0.5391 | 0.0067 | 0.4919 | 0.0112 | 0.5078 |
| 2024 | 0.0554 | 0.5352 | 0.0238 | 0.5509 | 0.0509 | 0.5585 |
| 2025 | -0.0713 | 0.4273 | -0.0292 | 0.4290 | 0.0072 | 0.4061 |
| 2026 | 0.1315 | 0.4752 | 0.1297 | 0.4617 | 0.0820 | 0.4875 |

#### momentum

| Year | H5_mean | H5_std | H10_mean | H10_std | H20_mean | H20_std |
|------|---------|--------|----------|---------|----------|---------|
| 2019 | 0.0500 | 0.4544 | -0.0160 | 0.4157 | -0.0158 | 0.4883 |
| 2020 | -0.0494 | 0.3969 | -0.0206 | 0.3799 | -0.0006 | 0.3715 |
| 2021 | -0.0102 | 0.4491 | 0.0410 | 0.4126 | 0.0355 | 0.3678 |
| 2022 | 0.0619 | 0.4078 | 0.0253 | 0.3905 | -0.0531 | 0.3814 |
| 2023 | -0.0957 | 0.3213 | -0.0454 | 0.3241 | -0.0437 | 0.3553 |
| 2024 | 0.0772 | 0.4162 | 0.0493 | 0.4104 | 0.0372 | 0.3412 |
| 2025 | 0.0334 | 0.3694 | 0.0755 | 0.3548 | 0.0913 | 0.3608 |
| 2026 | 0.1551 | 0.4681 | 0.2471 | 0.4610 | 0.1805 | 0.3479 |

#### volume

| Year | H5_mean | H5_std | H10_mean | H10_std | H20_mean | H20_std |
|------|---------|--------|----------|---------|----------|---------|
| 2019 | -0.0032 | 0.3699 | -0.0576 | 0.3774 | 0.0101 | 0.3647 |
| 2020 | -0.0020 | 0.3639 | -0.0070 | 0.3707 | -0.0238 | 0.3525 |
| 2021 | -0.0365 | 0.3356 | -0.0123 | 0.3224 | -0.0352 | 0.3165 |
| 2022 | -0.0117 | 0.3195 | 0.0049 | 0.3193 | -0.0087 | 0.3146 |
| 2023 | 0.0252 | 0.2606 | 0.0082 | 0.2594 | -0.0035 | 0.2561 |
| 2024 | 0.0424 | 0.2843 | 0.0219 | 0.2751 | 0.0420 | 0.2679 |
| 2025 | -0.0677 | 0.2787 | -0.0248 | 0.2797 | -0.0087 | 0.2600 |
| 2026 | -0.0194 | 0.2849 | 0.0031 | 0.2956 | 0.0458 | 0.2935 |

#### volatility

| Year | H5_mean | H5_std | H10_mean | H10_std | H20_mean | H20_std |
|------|---------|--------|----------|---------|----------|---------|

## 3. Year-by-Year Backtest Comparison: B0.1 vs no_momentum

*Training years: 2019-2022 | Validation years: 2023-2024*

| Year | Strategy | Ann.Return | Sharpe | Max DD | Trades | Days Invested | Turnover |
|------|----------|------------|--------|--------|--------|---------------|----------|
| 2019 (train) | B0.1 | 10.65% | 0.790 | -6.33% | 41 | 93 | 0.44 |
| 2019 (train) | no_momentum | 12.22% | 0.883 | -6.90% | 45 | 93 | 0.48 |
| 2020 (train) | B0.1 | 16.28% | 0.704 | -15.28% | 121 | 228 | 0.53 |
| 2020 (train) | no_momentum | 10.16% | 0.453 | -15.13% | 132 | 228 | 0.58 |
| 2021 (train) | B0.1 | -1.32% | -0.071 | -15.57% | 146 | 229 | 0.64 |
| 2021 (train) | no_momentum | -1.58% | -0.087 | -15.43% | 146 | 229 | 0.64 |
| 2022 (train) | B0.1 | 2.74% | 0.214 | -13.17% | 87 | 205 | 0.42 |
| 2022 (train) | no_momentum | 3.05% | 0.240 | -12.43% | 88 | 205 | 0.43 |
| 2023 (valid) | B0.1 | -1.01% | -0.062 | -18.32% | 132 | 237 | 0.56 |
| 2023 (valid) | no_momentum | 3.32% | 0.201 | -16.20% | 126 | 237 | 0.53 |
| 2024 (valid) | B0.1 | 21.97% | 0.945 | -12.52% | 110 | 242 | 0.45 |
| 2024 (valid) | no_momentum | 23.50% | 1.067 | -11.41% | 105 | 242 | 0.43 |

## 4. Training-Period Degradation Check

| Metric | B0.1 | no_momentum | Delta (B0.1 - no_momentum) | Degraded? |
|--------|------|-------------|------------------------|-----------|
| Avg Annual Return (training) | 7.09% | 5.96% | 1.12% | NO |

**Result**: `no_momentum` is **not degraded** in training (≤2 pp).

## 5. Recommendation

**Final recommendation**: `no_momentum enters final validation`

### Supporting evidence

- **Training avg return**: B0.1 = 7.09%, no_momentum = 5.96%
- **Validation avg return**: B0.1 = 10.48%, no_momentum = 13.41%
- **Degradation rule triggered**: No

## 6. Conclusion

1. `vol_score` is broken by design: the annualized volatility scale is
   incompatible with the hard-coded thresholds `[0.01, 0.04]` and `(0.04, 0.06]` .
   It contributes 0 points on virtually every day and should be repaired or removed.

2. Rank IC analysis shows the relative predictive power of each factor.
   Factors with consistently positive IR and low annual variance are more reliable.

3. The year-by-year comparison and the 2 pp degradation rule provide a disciplined
   framework for deciding whether to drop the momentum factor.

4. **Single recommendation**: `no_momentum enters final validation` .

---
*No production config (`src/config.py`) was modified. No final OOS test was run.*
