# 设计说明

## 目标

用离线、确定性的方式评估客服写操作策略，避免只有单元测试、没有业务边界回归。评测对象是 `policies/` 中的退款、优惠券、改地址、催单、工单、备注和权限规则。

## 评测口径

用例标签来自一版目标策略配置：

| 字段 | 目标值 |
|---|---:|
| 退款单笔上限 | 3000 |
| 退款日次数 | 2 |
| 退款日累计 | 6000 |
| 券面额上限 | 600 |
| 券延迟天数 | 2 |
| 券有效期 | 30 |

基线代码默认值是 2198 / 4396 / 1 / 439.60 / 3 / 30，因此会在边界样本上失败。这样评测不是复述当前实现，而是能发现策略与目标口径的偏差。

## 用例分布

| 类别 | 用例数 | 说明 |
|---|---:|---|
| refund | 20 | 金额边界、日次数/累计、状态、重复退款、归属 |
| coupon | 15 | 延迟窗口、券面额、已发券、发货时间缺失、归属 |
| address | 8 | 待支付/已发货/签收、归属 |
| urge | 6 | 已发货/未发货/签收、归属 |
| ticket | 5 | 会话归属、订单归属 |
| note | 3 | 会话归属 |
| permission | 3 | 非本人退款/改地址/工单 |

dev 30 / test 30，test 只做门控，不参与编辑。

## 闭环

```text
诊断 Agent（失败类型归因）
  -> 编辑 Agent（生成 6 个阈值的 JSON diff）
  -> dev 回归（quality 必须提升，核心类别不能下降）
  -> test 门控（不能退化）
  -> 通过则写 config.json
  -> 失败则拒绝并记录
```

允许修改的字段只有 `CSConfig` 的 6 个阈值。模型不能改业务代码、规则函数或评测集。

## 自动应用

`--auto-deploy` 在 dev/test 通过后执行：

1. 备份 `deploy/live_policy.json` 到 `deploy/backups/`；
2. 原子写入 `deploy/live_policy.json`；
3. 生成 `deploy/live_policy.env`；
4. 全量 60 题 smoke；
5. smoke 不达标时恢复旧配置；
6. 追加 `deploy/deployments.jsonl`。

注意：这里自动生效的是 staging artifact，不会自动重启 app/容器。生产使用还应加 reload 钩子、健康检查和金丝雀。

## 文件

| 文件 | 作用 |
|---|---|
| `cases.jsonl` | 用例集 |
| `build_cases.py` | 生成用例和 expected level |
| `cs_rsi.py` | 评测、闭环、部署 |
| `rsi_workspace/config.json` | 当前提交配置 |
| `rsi_workspace/history/` | 每轮诊断、候选 diff、指标和决策 |
| `deploy/` | 部署配置、备份、部署记录 |
| `results/report.md` | 人读结果 |
| `results/cs_rsi_*.json` | 机器可读结果 |

## 边界

这是任务级 RSI 最小闭环，不是完整 RSI：

- 编辑对象只有 6 个阈值；
- 没有递归改进“改进方法本身”；
- 没有自动修改代码或上线；
- 自动应用只写 staging 配置。

它证明的是：模型可以提出策略修改，但必须被固定 dev/test 回归门控约束。
