# 客服策略评测与 RSI 最小闭环

这是一个**离线、确定性**的客服策略评测：

- 不启动 FastAPI、不连 PostgreSQL、不调用线上模型；
- 直接用 `policies/` 的规则函数跑退款、优惠券、改地址、催单、工单、备注和权限用例；
- 标签来自一版目标策略配置（退款 3000/日累计 6000/日次数 2、券上限 600/延迟 2 天），用来模拟业务专家给出的边界。

## 文件

| 文件 | 作用 |
|---|---|
| `cases.jsonl` | 60 条用例，dev 30 / test 30 |
| `build_cases.py` | 用例生成脚本，说明标签口径 |
| `cs_rsi.py` | 诊断 Agent → 编辑 Agent → dev 回归 → test 门控 → 回滚/写入 |
| `rsi_workspace/config.json` | 当前已提交的 6 个策略阈值 |
| `rsi_workspace/history/` | 每轮诊断、候选 diff、dev/test 和决策 |
| `deploy/live_policy.json` | 自动应用后的 staging 策略配置 |
| `deploy/deployments.jsonl` | 每次自动应用的 smoke、backup 和状态 |
| `results/report.md` | 最近一次结果 |
| `results/cs_rsi_*.json` | 机器可读结果 |

## 运行

```bash
cd cs-agent
python eval/cs_rsi/build_cases.py
python eval/cs_rsi/cs_rsi.py --model qwen3.5:4b-q4_K_M --rounds 3 --reset
```

需要自动应用时：

```bash
python eval/cs_rsi/cs_rsi.py --model qwen3.5:4b-q4_K_M --rounds 3 --reset --auto-deploy
```

`--auto-deploy` 会做：

1. 通过 dev/test 门控；
2. 备份旧的 `deploy/live_policy.json`；
3. 原子写入新的 `deploy/live_policy.json` 和 `deploy/live_policy.env`；
4. 用全量 60 题做 smoke；
5. smoke 不达标就恢复旧配置，并写入 `deploy/deployments.jsonl`。

它**不会**自动重启 app/容器；真实生产还需要 reload/重启、健康检查和金丝雀。

Windows 本地如果没有时区库，`requirements.txt` 已增加：

```text
tzdata; platform_system == "Windows"
```

## 评测覆盖

| 类别 | 用例数 | 说明 |
|---|---:|---|
| refund | 20 | 金额边界、日次数/累计、状态、重复退款、归属 |
| coupon | 15 | 延迟窗口、券面额、已发券、发货时间缺失、归属 |
| address | 8 | 待支付/已发货/签收、归属 |
| urge | 6 | 已发货/未发货/签收、归属 |
| ticket | 5 | 会话归属、订单归属 |
| note | 3 | 会话归属 |
| permission | 3 | 非本人退款/改地址/工单 |

## 本次结果

基线配置：

```json
{
  "refund_amount_limit": 2198.0,
  "refund_daily_max_count": 1,
  "refund_daily_max_total": 4396.0,
  "coupon_amount_limit": 439.6,
  "coupon_delay_days": 3,
  "coupon_expire_days": 30
}
```

模型 `qwen3.5:4b-q4_K_M` 第一轮诊断为退款额度/累计额度和券延迟窗口问题，提出：

```json
{
  "refund_amount_limit": 3000.0,
  "refund_daily_max_count": 2,
  "refund_daily_max_total": 6000.0,
  "coupon_amount_limit": 600.0,
  "coupon_delay_days": 2,
  "coupon_expire_days": 30
}
```

结果：

| 阶段 | dev | test |
|---|---:|---:|
| baseline | 86.67% | 86.67% |
| round 1（接受） | **100%** | **100%** |
| round 2 / 3 | 100% | — |
| round 2 / 3 决策 | 拒绝：dev 没有再提升 | — |

round 1 通过 test 门控后开启 `--auto-deploy`：写入 `deploy/live_policy.json/.env`，全量 smoke=100%，备份旧配置到 `deploy/backups/`。这是 staging artifact，不会自动重启 app。

## 它不是完整 RSI

- 只编辑 6 个阈值，不改规则函数；
- 没有递归改进“改进方法本身”；
- `--auto-deploy` 只写 staging artifact，不自动重启生产服务；
- test 只用于门控，不参与编辑；
- 真实生产还需要人工审批、版本回滚、金丝雀发布和线上监控。

这个目录的价值是证明：**模型可以提出策略修改，但必须被固定 dev/test 回归门控约束。**
