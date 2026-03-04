# billcount

一个面向**现货 + 期货基差交易**的本地记账工具（SQLite + Python CLI）。

支持能力：
- 记录现货/期货每笔成交
- 按 FIFO 自动计算已实现和未实现盈亏
- 汇总手续费
- 每日记录资金成本：
  - 现货：按持货本金计息，卖出后对应份额停止计息
  - 期货：按账户累计入金（可含出金）计息
- 按需导出每日资金成本 CSV

默认年化资金利率为 **6.5%**。

## 运行环境

- Python 3.10+
- 无第三方依赖（仅标准库）

## 快速开始

```bash
python3 basis_accounting.py --db demo.db init
```

## 交易记录

### 1) 记录现货交易

```bash
python3 basis_accounting.py --db demo.db add --market spot --symbol RB --side buy --qty 100 --price 3500 --fee 20 --time 2026-01-02T10:00:00
python3 basis_accounting.py --db demo.db add --market spot --symbol RB --side sell --qty 30 --price 3560 --fee 8 --time 2026-01-05T10:00:00
```

### 2) 记录期货交易

```bash
python3 basis_accounting.py --db demo.db add --market futures --symbol RB2410 --side sell --qty 100 --price 3550 --fee 25 --time 2026-01-02T10:01:00
```

### 3) 记录期货账户资金流水（累计入金）

```bash
python3 basis_accounting.py --db demo.db add-cashflow --amount 500000 --note 初始入金 --time 2026-01-02T09:00:00
python3 basis_accounting.py --db demo.db add-cashflow --amount -100000 --note 部分出金 --time 2026-01-10T09:00:00
```

## 生成报告

```bash
python3 basis_accounting.py --db demo.db report \
  --spot-mark RB=3520 \
  --futures-mark RB2410=3530
```

## 导出每日资金成本 CSV

```bash
python3 basis_accounting.py --db demo.db export-interest-csv \
  --output exports/interest_2026-01.csv \
  --start-date 2026-01-01 \
  --end-date 2026-01-31
```

导出字段：
- `date`
- `spot_principal`（现货持仓本金）
- `spot_interest`（现货当日利息）
- `futures_principal`（期货账户计息本金，基于累计入金）
- `futures_interest`（期货当日利息）
- `total_interest`（当日合计资金成本）

## 常用命令

```bash
python3 basis_accounting.py --db demo.db list
python3 basis_accounting.py --db demo.db list-cashflow
python3 basis_accounting.py --db demo.db --help
```
