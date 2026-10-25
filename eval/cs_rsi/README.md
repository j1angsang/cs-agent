# 客服策略评测与 RSI 最小闭环

一套离线、确定性的客服策略评测与自动校准工具，覆盖退款、优惠券、改地址、催单、工单、备注和权限。

它不启动 FastAPI、不连 PostgreSQL，也不依赖线上模型；用于在改策略阈值前先跑固定回归集。

## 能做什么

- 60 条确定性策略用例，dev/test 各 30 条
- 诊断失败 → 编辑策略阈值 → dev 回归 → test 门控 → 回滚/写入
- 可选自动应用：备份旧配置、全量 smoke、失败恢复
- 可选本地 Ollama 编辑器；不接模型也能跑基线

## 快速开始

环境：Python 3.10+。Windows 本地还需要 `tzdata`，已写入 `requirements.txt`。

```bash
cd cs-agent
python eval/cs_rsi/build_cases.py
python eval/cs_rsi/cs_rsi.py --model qwen3.5:4b-q4_K_M --rounds 3 --reset
```

只想跑确定性基线、不调用模型时：

```bash
python eval/cs_rsi/cs_rsi.py --rounds 0
```

### 自动应用

```bash
python eval/cs_rsi/cs_rsi.py --model qwen3.5:4b-q4_K_M --rounds 3 --reset --auto-deploy
```

通过 dev/test 门控后会：

- 备份旧配置
- 写入 `deploy/live_policy.json` 和 `deploy/live_policy.env`
- 用全量 60 题做 smoke
- smoke 失败则恢复旧配置
- 记录到 `deploy/deployments.jsonl`

这是 staging artifact，不会自动重启 app/容器。

## 结果

| 阶段 | dev | test |
|---|---:|---:|
| baseline | 86.67% | 86.67% |
| 自动校准后 | **100%** | **100%** |

本次模型为 `qwen3.5:4b-q4_K_M`，自动部署 smoke 为 100%。完整逐轮记录见 [results/report.md](results/report.md)。

## 目录

| 路径 | 内容 |
|---|---|
| `cases.jsonl` | 60 条策略用例 |
| `build_cases.py` | 用例生成脚本 |
| `cs_rsi.py` | 评测与最小闭环入口 |
| `rsi_workspace/` | 当前配置、历史轮次 |
| `deploy/` | 自动应用配置、备份和部署记录 |
| `results/` | 报告和机器可读结果 |

## 边界

- 只校准 6 个策略阈值，不改规则函数；
- 不是完整 RSI，没有递归改进“改进方法本身”；
- test 只做门控，不参与编辑；
- 自动应用只写 staging 配置，不负责重启生产服务。

## 相关文档

- [DESIGN.md](DESIGN.md)：数据口径、配置字段、闭环实现和自动应用细节
- [results/report.md](results/report.md)：最近一次逐轮结果
- [../README.md](../README.md)：Agent 七维评测
