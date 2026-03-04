#!/usr/bin/env python3
"""本地基差交易记账系统（现货 + 期货）。"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_time TEXT NOT NULL,
    market TEXT NOT NULL CHECK (market IN ('spot', 'futures')),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity REAL NOT NULL CHECK (quantity > 0),
    price REAL NOT NULL CHECK (price >= 0),
    fee REAL NOT NULL DEFAULT 0,
    multiplier REAL NOT NULL DEFAULT 1,
    margin_rate REAL NOT NULL DEFAULT 0.1
);

CREATE TABLE IF NOT EXISTS futures_cash_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_time TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
"""


@dataclass
class Trade:
    trade_time: str
    market: str
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float
    multiplier: float
    margin_rate: float


@dataclass
class CashFlow:
    flow_time: str
    amount: float
    note: str


class BasisLedger:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)

    def add_trade(self, trade: Trade) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trades (
                    trade_time, market, symbol, side, quantity, price, fee, multiplier, margin_rate
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.trade_time,
                    trade.market,
                    trade.symbol,
                    trade.side,
                    trade.quantity,
                    trade.price,
                    trade.fee,
                    trade.multiplier,
                    trade.margin_rate,
                ),
            )

    def add_cash_flow(self, cash_flow: CashFlow) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO futures_cash_flows (flow_time, amount, note)
                VALUES (?, ?, ?)
                """,
                (cash_flow.flow_time, cash_flow.amount, cash_flow.note),
            )

    def list_trades(self) -> List[Tuple]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT id, trade_time, market, symbol, side, quantity, price, fee, multiplier, margin_rate
                FROM trades
                ORDER BY trade_time, id
                """
            )
            return cur.fetchall()

    def list_cash_flows(self) -> List[Tuple]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT id, flow_time, amount, note
                FROM futures_cash_flows
                ORDER BY flow_time, id
                """
            )
            return cur.fetchall()

    def load_by_market(self, market: str) -> Dict[str, List[Trade]]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT trade_time, market, symbol, side, quantity, price, fee, multiplier, margin_rate
                FROM trades
                WHERE market = ?
                ORDER BY trade_time, id
                """,
                (market,),
            )
            rows = cur.fetchall()
        grouped: Dict[str, List[Trade]] = {}
        for row in rows:
            t = Trade(*row)
            grouped.setdefault(t.symbol, []).append(t)
        return grouped

    def load_spot_trades(self) -> List[Trade]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT trade_time, market, symbol, side, quantity, price, fee, multiplier, margin_rate
                FROM trades
                WHERE market = 'spot'
                ORDER BY trade_time, id
                """
            )
            return [Trade(*row) for row in cur.fetchall()]

    def load_cash_flows(self) -> List[CashFlow]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT flow_time, amount, note
                FROM futures_cash_flows
                ORDER BY flow_time, id
                """
            )
            return [CashFlow(*row) for row in cur.fetchall()]


def _to_date(ts: str) -> date:
    return datetime.fromisoformat(ts).date()


def _fifo_realized_unrealized(trades: Iterable[Trade], mark_price: float) -> Dict[str, float]:
    long_lots: List[List[float]] = []
    short_lots: List[List[float]] = []
    realized = 0.0
    fee_total = 0.0
    multiplier = 1.0

    for t in trades:
        qty = t.quantity
        price = t.price
        multiplier = t.multiplier
        fee_total += t.fee

        if t.side == "buy":
            while qty > 1e-12 and short_lots:
                s_qty, s_price = short_lots[0]
                matched = min(qty, s_qty)
                realized += (s_price - price) * matched * multiplier
                s_qty -= matched
                qty -= matched
                if s_qty <= 1e-12:
                    short_lots.pop(0)
                else:
                    short_lots[0][0] = s_qty
            if qty > 1e-12:
                long_lots.append([qty, price])
        else:
            while qty > 1e-12 and long_lots:
                l_qty, l_price = long_lots[0]
                matched = min(qty, l_qty)
                realized += (price - l_price) * matched * multiplier
                l_qty -= matched
                qty -= matched
                if l_qty <= 1e-12:
                    long_lots.pop(0)
                else:
                    long_lots[0][0] = l_qty
            if qty > 1e-12:
                short_lots.append([qty, price])

    long_qty = sum(q for q, _ in long_lots)
    short_qty = sum(q for q, _ in short_lots)
    long_cost = sum(q * p for q, p in long_lots)
    short_cost = sum(q * p for q, p in short_lots)

    long_avg = long_cost / long_qty if long_qty else 0.0
    short_avg = short_cost / short_qty if short_qty else 0.0

    unrealized = (mark_price - long_avg) * long_qty * multiplier
    unrealized += (short_avg - mark_price) * short_qty * multiplier

    net_qty = long_qty - short_qty
    return {
        "realized": realized,
        "unrealized": unrealized,
        "fees": fee_total,
        "net_qty": net_qty,
        "multiplier": multiplier,
    }


def _daterange(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _build_daily_interest_rows(
    ledger: BasisLedger,
    annual_rate: float,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> List[Dict[str, object]]:
    spot_trades = ledger.load_spot_trades()
    cash_flows = ledger.load_cash_flows()

    event_dates: List[date] = []
    if spot_trades:
        event_dates.extend(_to_date(t.trade_time) for t in spot_trades)
    if cash_flows:
        event_dates.extend(_to_date(f.flow_time) for f in cash_flows)
    if not event_dates:
        return []

    start = start_date or min(event_dates)
    end = end_date or max(event_dates)
    if start > end:
        return []

    spot_events: Dict[date, List[Trade]] = {}
    for t in spot_trades:
        d = _to_date(t.trade_time)
        spot_events.setdefault(d, []).append(t)

    flow_events: Dict[date, List[CashFlow]] = {}
    for f in cash_flows:
        d = _to_date(f.flow_time)
        flow_events.setdefault(d, []).append(f)

    open_spot_lots: Dict[str, List[List[float]]] = {}
    futures_principal = 0.0
    rows: List[Dict[str, object]] = []

    for day in _daterange(start, end):
        for t in spot_events.get(day, []):
            lots = open_spot_lots.setdefault(t.symbol, [])
            if t.side == "buy":
                lots.append([t.quantity, t.price, t.multiplier])
            else:
                qty = t.quantity
                while qty > 1e-12 and lots:
                    l_qty, l_price, l_mul = lots[0]
                    matched = min(qty, l_qty)
                    l_qty -= matched
                    qty -= matched
                    if l_qty <= 1e-12:
                        lots.pop(0)
                    else:
                        lots[0][0] = l_qty

        for f in flow_events.get(day, []):
            futures_principal += f.amount

        spot_principal = 0.0
        for lots in open_spot_lots.values():
            spot_principal += sum(qty * price * mul for qty, price, mul in lots)

        spot_interest = spot_principal * annual_rate / 365
        futures_interest = futures_principal * annual_rate / 365

        rows.append(
            {
                "date": day.isoformat(),
                "spot_principal": round(spot_principal, 2),
                "spot_interest": round(spot_interest, 2),
                "futures_principal": round(futures_principal, 2),
                "futures_interest": round(futures_interest, 2),
                "total_interest": round(spot_interest + futures_interest, 2),
            }
        )

    return rows


def export_daily_interest_csv(
    ledger: BasisLedger,
    output_path: str,
    annual_rate: float,
    start_date: Optional[date],
    end_date: Optional[date],
) -> int:
    rows = _build_daily_interest_rows(ledger, annual_rate, start_date, end_date)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "date",
                "spot_principal",
                "spot_interest",
                "futures_principal",
                "futures_interest",
                "total_interest",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def make_report(
    ledger: BasisLedger,
    spot_mark: Dict[str, float],
    futures_mark: Dict[str, float],
    annual_funding_rate: float,
) -> str:
    lines: List[str] = []
    total_realized = 0.0
    total_unrealized = 0.0
    total_fees = 0.0

    lines.append("=== 基差交易记账报告 ===")

    spot = ledger.load_by_market("spot")
    lines.append("\n[现货]")
    if not spot:
        lines.append("- 无交易")
    for symbol, trades in spot.items():
        mark = spot_mark.get(symbol, trades[-1].price)
        m = _fifo_realized_unrealized(trades, mark)
        total_realized += m["realized"]
        total_unrealized += m["unrealized"]
        total_fees += m["fees"]
        lines.append(
            f"- {symbol}: 净持仓={m['net_qty']:.4f}, 已实现={m['realized']:.2f}, 未实现={m['unrealized']:.2f}, 手续费={m['fees']:.2f}, 估值价={mark:.2f}"
        )

    fut = ledger.load_by_market("futures")
    lines.append("\n[期货]")
    if not fut:
        lines.append("- 无交易")
    for symbol, trades in fut.items():
        mark = futures_mark.get(symbol, trades[-1].price)
        m = _fifo_realized_unrealized(trades, mark)
        total_realized += m["realized"]
        total_unrealized += m["unrealized"]
        total_fees += m["fees"]
        lines.append(
            f"- {symbol}: 净持仓={m['net_qty']:.4f}, 已实现={m['realized']:.2f}, 未实现={m['unrealized']:.2f}, 手续费={m['fees']:.2f}, 估值价={mark:.2f}"
        )

    daily_rows = _build_daily_interest_rows(ledger, annual_funding_rate)
    total_interest = sum(float(r["total_interest"]) for r in daily_rows)

    lines.append("\n[每日资金成本]")
    if not daily_rows:
        lines.append("- 无可计息数据")
    else:
        lines.append(f"- 计息天数: {len(daily_rows)}")
        lines.append(f"- 累计资金成本: {total_interest:.2f}")
        lines.append(
            f"- 最近一日({daily_rows[-1]['date']}): 现货本金={daily_rows[-1]['spot_principal']:.2f}, 期货本金={daily_rows[-1]['futures_principal']:.2f}, 当日利息={daily_rows[-1]['total_interest']:.2f}"
        )

    net_pnl = total_realized + total_unrealized - total_fees - total_interest

    lines.append("\n[汇总]")
    lines.append(f"- 已实现盈亏: {total_realized:.2f}")
    lines.append(f"- 未实现盈亏: {total_unrealized:.2f}")
    lines.append(f"- 手续费: {total_fees:.2f}")
    lines.append(f"- 资金成本(年化{annual_funding_rate:.2%}): {total_interest:.2f}")
    lines.append(f"- 净盈亏(扣费+资金成本): {net_pnl:.2f}")
    return "\n".join(lines)


def _parse_marks(items: List[str]) -> Dict[str, float]:
    marks: Dict[str, float] = {}
    for item in items:
        symbol, price = item.split("=", 1)
        marks[symbol.strip()] = float(price)
    return marks


def _parse_date(date_str: Optional[str]) -> Optional[date]:
    if not date_str:
        return None
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="期货/现货基差交易本地记账系统")
    parser.add_argument("--db", default="basis_ledger.db", help="SQLite 数据库路径")

    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化数据库")

    add = sub.add_parser("add", help="新增一笔交易")
    add.add_argument("--market", choices=["spot", "futures"], required=True)
    add.add_argument("--symbol", required=True)
    add.add_argument("--side", choices=["buy", "sell"], required=True)
    add.add_argument("--qty", type=float, required=True)
    add.add_argument("--price", type=float, required=True)
    add.add_argument("--fee", type=float, default=0.0)
    add.add_argument("--multiplier", type=float, default=1.0)
    add.add_argument("--margin-rate", type=float, default=0.1)
    add.add_argument("--time", default=datetime.now().isoformat(timespec="seconds"))

    cash = sub.add_parser("add-cashflow", help="记录期货账户入金/出金")
    cash.add_argument("--amount", type=float, required=True, help="入金为正，出金为负")
    cash.add_argument("--note", default="", help="备注")
    cash.add_argument("--time", default=datetime.now().isoformat(timespec="seconds"))

    sub.add_parser("list", help="列出所有交易")
    sub.add_parser("list-cashflow", help="列出期货账户资金流水")

    rep = sub.add_parser("report", help="生成盈亏/资金报告")
    rep.add_argument("--spot-mark", nargs="*", default=[], help="格式: BTC=70500")
    rep.add_argument("--futures-mark", nargs="*", default=[], help="格式: BTCUSD-PERP=70200")
    rep.add_argument("--annual-rate", type=float, default=0.065, help="资金年化成本，默认 6.5%")

    exp = sub.add_parser("export-interest-csv", help="导出每日资金成本CSV")
    exp.add_argument("--output", required=True, help="输出 CSV 文件路径")
    exp.add_argument("--annual-rate", type=float, default=0.065, help="资金年化成本，默认 6.5%")
    exp.add_argument("--start-date", help="开始日期，格式 YYYY-MM-DD")
    exp.add_argument("--end-date", help="结束日期，格式 YYYY-MM-DD")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    ledger = BasisLedger(args.db)

    if args.cmd == "init":
        ledger.init_db()
        print(f"数据库已初始化: {args.db}")
        return

    ledger.init_db()

    if args.cmd == "add":
        trade = Trade(
            trade_time=args.time,
            market=args.market,
            symbol=args.symbol,
            side=args.side,
            quantity=args.qty,
            price=args.price,
            fee=args.fee,
            multiplier=args.multiplier,
            margin_rate=args.margin_rate,
        )
        ledger.add_trade(trade)
        print("交易已记录")
    elif args.cmd == "add-cashflow":
        ledger.add_cash_flow(CashFlow(flow_time=args.time, amount=args.amount, note=args.note))
        print("期货资金流水已记录")
    elif args.cmd == "list":
        for row in ledger.list_trades():
            print(row)
    elif args.cmd == "list-cashflow":
        for row in ledger.list_cash_flows():
            print(row)
    elif args.cmd == "report":
        spot_mark = _parse_marks(args.spot_mark)
        futures_mark = _parse_marks(args.futures_mark)
        print(make_report(ledger, spot_mark, futures_mark, args.annual_rate))
    elif args.cmd == "export-interest-csv":
        rows = export_daily_interest_csv(
            ledger,
            output_path=args.output,
            annual_rate=args.annual_rate,
            start_date=_parse_date(args.start_date),
            end_date=_parse_date(args.end_date),
        )
        print(f"已导出 {rows} 条每日资金成本记录到: {args.output}")


if __name__ == "__main__":
    main()
