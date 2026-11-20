# 档案留置簿业务与测试登记

- 规模：中型
- 使用者：企业档案室保管员
- 核心规则：按结案日期匹配保管版本，按周年计期，再越过覆盖最早处置日的连续留置链。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 移交清单 | 检查编码、字段宽度和来源行 | `POST /api/intake` | `app/modules/intake/service.py` | `tests/test_intake.py` |
| M2 政策字段 | 归一修订、业务键和偏移时间 | `POST /api/contract` | `app/modules/contract/service.py` | `tests/test_contract.py` |
| M3 档案异常 | 定位重复、被替代与矛盾来源 | `POST /api/quality` | `app/modules/quality/versions.py` | `tests/test_quality.py` |
| M4 处置日期 | 按规则复算并追溯证据 | `POST /api/domain` | `app/modules/domain/rule.py` | `tests/test_domain.py` |
| M5 保管台账 | 核对独立清单和各类差异 | `POST /api/reconcile` | `app/modules/reconcile/service.py` | `tests/test_reconcile.py` |
| M6 凭据核销 | 跨批次认领、争用、退回与重新认领凭据 | `POST /api/settle/{run_id}/claim` | `app/modules/settle/service.py` | `tests/test_settle.py` |
| M7 留置批次 | 打开历史批次并对原文件复跑 | `POST /api/runs` | `app/modules/runs/service.py` | `tests/test_runs.py` |
| M8 处置复核 | 登记复核结论并下载证据 | `POST /api/review/{run_id}` | `app/modules/review/decisions.py` | `tests/test_review.py` |

## 陷阱登记

| TRAP | 现象 | 样例 | 处理位置 | 测试 |
|---|---|---|---|---|
| TRAP-01 | GB18030 来源文件 | legacy.csv | `app/modules/intake/reader.py` | `tests/test_intake.py::test_gb18030` |
| TRAP-02 | BOM 前缀 | shift.csv | `app/modules/intake/reader.py` | `tests/test_intake.py::test_bom` |
| TRAP-03 | 备注含引号内换行 | shift.csv | `app/modules/intake/service.py` | `tests/test_intake.py::test_multiline` |
| TRAP-04 | 分段重复表头 | shift.csv | `app/modules/intake/service.py` | `tests/test_intake.py::test_repeated` |
| TRAP-05 | 列数不足 | shift.csv | `app/modules/intake/service.py` | `tests/test_intake.py::test_width` |
| TRAP-07 | 缺测哨兵 | shift.csv | `app/modules/contract/service.py` | `tests/test_contract.py::test_missing` |
| TRAP-08 | 前导零的单号 | shift.csv | `app/modules/contract/service.py` | `tests/test_contract.py::test_leading_zero` |
| TRAP-11 | 偏移时刻 UTC 归一 | shift.csv | `app/modules/domain/values.py` | `tests/test_contract.py::test_offsets` |
| TRAP-14 | 同版业务字段冲突 | shift.csv | `app/modules/quality/versions.py` | `tests/test_quality.py::test_conflicting` |
| TRAP-16 | 末行引号截断 | truncated.csv | `app/modules/intake/reader.py` | `tests/test_intake.py::test_truncated_rejected` |
| TRAP-17 | 旧版列名映射 | legacy.csv | `app/modules/contract/schema.py` | `tests/test_contract.py::test_legacy_aliased` |
| TRAP-18 | 空文件、空白文件与仅表头 | 测试构造 | `app/modules/intake/service.py` | `tests/test_intake.py::test_empty_states` |
| TRAP-26 | 陈旧复核版本拒绝 | 测试构造 | `app/modules/review/decisions.py` | `tests/test_review.py::test_stale` |
| TRAP-31 | 同号外部凭据两批争用，先到先核销 | 测试构造 | `app/modules/settle/service.py` | `tests/test_settle.py::test_first_claim_wins_and_second_is_contended` |
| TRAP-32 | 重试确认不得重复核销 | 测试构造 | `app/modules/settle/service.py` | `tests/test_settle.py::test_retry_is_idempotent_and_never_double_writes` |
| TRAP-33 | 并发同刻争用只允许一笔核销 | 测试构造 | `app/modules/settle/repository.py` | `tests/test_settle.py::test_concurrent_claims_only_one_write_off` |
| TRAP-34 | 退回后释放凭据并可重新认领 | 测试构造 | `app/modules/settle/service.py` | `tests/test_settle.py::test_return_allows_reclaim_and_keeps_full_trail` |
| TRAP-25 | ambiguous policy | cases.csv | `app/modules/domain/rule.py` | `tests/test_domain.py::test_ambiguous_policy` |
| TRAP-23 | adjacent holds | cases.csv | `app/modules/domain/rule.py` | `tests/test_domain.py::test_adjacent_holds` |

## 对账

输入 = 计入 + 隔离 + 重传及版本替换 + 空白记录 + 重复表头。按业务键全外连接，缺失一侧与数值为零分别登记。逐笔差值与总差一致。

## 凭据核销

每个批次落库时，把外部账中实际出现的业务键登记为该批次可认领的外部凭据（仅外部账一侧缺失 `MISSING_BOOK` 的键不登记）。

- 先到先得：同一凭据同时只允许一笔 `HELD` 核销，由部分唯一索引 `claim_active_once` 与 `BEGIN IMMEDIATE` 事务共同保证；晚到批次的认领以 `CLAIM_CONTENDED`（409）拒绝并登记 `CONTENDED` 事件，费用不会结两次。
- 归属未核清时先让先到批次当天结账，争用记录可在「核销与争用依据」和导出证据中追溯，等待双方复核。
- 重试确认：同一批次重复认领返回 `written_off=false` 的 `CLAIM_RETRY`，只追加 `RETRY_NOOP` 事件，不产生第二笔核销。
- 退回：只有持有批次可退回（`RETURN_DENIED` 记录越权退回），退回后凭据回到可认领状态，任一批次可 `RECLAIMED` 重新认领；`其他` 退回依据必须填写说明。
- 每个批次可查看它所对账凭据的完整事件链，包括其他批次写入的争用与退回事件。
