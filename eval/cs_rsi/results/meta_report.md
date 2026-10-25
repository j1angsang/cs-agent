# 客服策略外层 meta-loop

> 内层校准阈值，外层比较“只调退款 / 只调券 / 全调”三种改进策略；3 折 meta-validation 选择，test 只审计。

- 数据：61 题（dev 31 / test 30；3 折 meta CV）

| 策略 | 改动字段 | 各折 meta-val | 均值 | meta-score | 选中 |
|---|---:|---|---:|---:|---|
| refund_only | 3 | 0.8462 / 0.9091 / 1.0 | 0.9184 | 0.9184 |  |
| coupon_only | 1 | 0.8462 / 0.9091 / 0.7143 | 0.8232 | 0.8232 |  |
| full | 4 | 0.8462 / 0.9091 / 1.0 | 0.9184 | 0.9184 | 是 |

## 选择结果
- 选中策略：`full`
- 配置：`{"refund_amount_limit": 3000.0, "refund_daily_max_count": 2, "refund_daily_max_total": 6000.0, "coupon_amount_limit": 439.6, "coupon_delay_days": 2, "coupon_expire_days": 30}`
- meta-val 均值：0.9184
- test：1.0（gate=通过）
- 最终配置：`{"refund_amount_limit": 3000.0, "refund_daily_max_count": 2, "refund_daily_max_total": 6000.0, "coupon_amount_limit": 439.6, "coupon_delay_days": 2, "coupon_expire_days": 30}`

## 边界
- 策略空间是固定的 3 种，不是开放式自我改写；
- 3 折 meta-val 选策略，test 只做最终审计；
- 不自动上线。