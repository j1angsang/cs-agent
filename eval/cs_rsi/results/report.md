# 客服策略 RSI 最小闭环

> 诊断 Agent -> 编辑 Agent -> dev 回归 -> test 门控 -> 自动审批/回滚。
> 审稿的是退款、优惠券、地址、催单、工单、备注、权限等确定性策略。

- 模型：`qwen3.5:4b-q4_K_M`
- 用例：60（dev 30 / test 30）
- 最终配置：`{"refund_amount_limit": 3000.0, "refund_daily_max_count": 2, "refund_daily_max_total": 6000.0, "coupon_amount_limit": 600.0, "coupon_delay_days": 2, "coupon_expire_days": 30}`
- 最终 dev/test：1.0/1.0
- 自动部署：开启（写 deploy/live_policy.*）

## 每轮结果
| 轮次 | 候选改动 | dev | test | 决策 | 原因 |
|---|---|---:|---:|---|---|
| 1 | `{"refund_amount_limit": {"from": 2198.0, "to": 3000.0}, "refund_daily_max_count": {"fro...` | 1.0 | 1.0 | accepted | dev 提升且 test 门控通过 |
| 2 | `{}` | 1.0 | - | rejected | dev quality 未提升（1.0 -> 1.0） |
| 3 | `{}` | 1.0 | - | rejected | dev quality 未提升（1.0 -> 1.0） |

## 诊断摘要
- round 1: 样本中失败原因涵盖订单金额超出阈值、累计退款次数超限、订单状态异常及券使用延迟窗口限制
- round 2: 未检测到任何失败案例
- round 3: 未检测到任何失败样本

## 自动部署记录
- deployed：smoke=1.0，backup=deploy\backups\live_policy-20261026-004235.json

## 边界
- 不是完整 RSI：只校准 6 个策略阈值，不改规则函数；
- `--auto-deploy` 写的是 `deploy/live_policy.*` staging artifact，不会自动重启 app/容器；
- test 只做门控，不参与编辑；
- 真实生产还需要进程 reload、健康检查和金丝雀。