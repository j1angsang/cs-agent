# 客服策略 RSI 最小闭环

> 诊断 Agent -> 编辑 Agent -> dev 回归 -> test 门控 -> 自动审批/回滚。
> 审稿的是退款、优惠券、地址、催单、工单、备注、权限等确定性策略。

- 模型：`qwen3.5:4b-q4_K_M`
- 用例：60（dev 30 / test 30）
- 最终配置：`{"refund_amount_limit": 3000.0, "refund_daily_max_count": 2, "refund_daily_max_total": 6000.0, "coupon_amount_limit": 600.0, "coupon_delay_days": 2, "coupon_expire_days": 30}`
- 最终 dev/test：1.0/1.0

## 每轮结果
| 轮次 | 候选改动 | dev | test | 决策 | 原因 |
|---|---|---:|---:|---|---|
| 1 | `{"refund_amount_limit": {"from": 2198.0, "to": 3000.0}, "refund_daily_max_count": {"fro...` | 1.0 | 1.0 | accepted | dev 提升且 test 门控通过 |
| 2 | `{}` | 1.0 | - | rejected | dev quality 未提升（1.0 -> 1.0） |
| 3 | `{}` | 1.0 | - | rejected | dev quality 未提升（1.0 -> 1.0） |

## 诊断摘要
- round 1: 系统在处理退款和优惠券时因金额、累计次数及时间窗口等阈值条件未满足而拒绝自动操作
- round 2: 未检测到任何失败案例
- round 3: 未检测到任何失败案例

## 边界
- 不是完整 RSI：只校准 6 个策略阈值，不改规则函数，没有自动上线；
- test 只做门控，不参与编辑；
- 自动审批只用于演示，真实流程需要人工审批和回滚。