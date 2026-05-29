"""Generate a realistic ~100-row sample dataset for the Debt OS demo.

Produces:
  - sample_finances.xlsx  (two sheets: "Debts" + "Statement") — drop into the UI
  - debts.csv
  - bank_statement.csv

Run:  python sample_data/generate.py
"""
import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

random.seed(42)
HERE = Path(__file__).resolve().parent

# ---- Debts -------------------------------------------------------------
debts = pd.DataFrame([
    {"creditor": "Visa Classic",   "kind": "credit card",   "balance": 2400.0, "apr": 19.9, "min_payment": 60,  "due_day": 14},
    {"creditor": "Amex Gold",      "kind": "credit card",   "balance": 5200.0, "apr": 23.9, "min_payment": 130, "due_day": 2},
    {"creditor": "Klarna",         "kind": "BNPL",          "balance": 600.0,  "apr": 0.0,  "min_payment": 50,  "due_day": 28},
    {"creditor": "Car Loan",       "kind": "personal loan", "balance": 9800.0, "apr": 6.4,  "min_payment": 245, "due_day": 20},
    {"creditor": "Personal Loan",  "kind": "loan",          "balance": 4300.0, "apr": 11.5, "min_payment": 150, "due_day": 8},
    {"creditor": "Overdraft",      "kind": "overdraft",     "balance": 750.0,  "apr": 39.9, "min_payment": 20,  "due_day": 1},
])

# ---- Bank statement (~100 rows over 4 months) --------------------------
rows = []
# Four whole months ending last month, relative to a fixed "today".
today = date(2026, 5, 29)
start = date(today.year, today.month, 1) - timedelta(days=120)
first = date(start.year, start.month, 1)

groceries = ["Tesco", "Lidl", "Aldi", "SuperValu", "Dunnes"]
dining = ["Deliveroo", "Local Cafe", "Pizza Place", "Sushi Bar"]
transport = ["Fuel Station", "Bus Top-up", "Rail Ticket", "Taxi"]
subs = [("Netflix", 13.99), ("Spotify", 10.99), ("Gym", 39.0), ("Mobile", 25.0), ("Broadband", 45.0)]

def add(d, amount, cat, who):
    rows.append({"date": d.isoformat(), "description": who, "category": cat, "amount": round(amount, 2)})

month = first
for _ in range(4):
    y, m = month.year, month.month
    def D(day):
        return date(y, m, min(day, 28))
    add(D(25), 2900.0, "income", "ACME Payroll")          # salary
    add(D(1),  -1100.0, "housing", "Rent")                # rent
    add(D(3),  -95.0,  "utilities", "Electric & Gas")
    for name, amt in subs:                                # subscriptions
        add(D(random.randint(4, 12)), -amt, "subscription", name)
    # debt payments (match the minimums above)
    add(D(2),  -130.0, "debt", "Amex Gold payment")
    add(D(8),  -150.0, "debt", "Personal Loan payment")
    add(D(14), -60.0,  "debt", "Visa Classic payment")
    add(D(20), -245.0, "debt", "Car Loan payment")
    # variable spending
    for _ in range(random.randint(8, 10)):
        add(D(random.randint(2, 27)), -round(random.uniform(18, 85), 2), "groceries", random.choice(groceries))
    for _ in range(random.randint(4, 6)):
        add(D(random.randint(2, 27)), -round(random.uniform(9, 40), 2), "dining", random.choice(dining))
    for _ in range(random.randint(3, 4)):
        add(D(random.randint(2, 27)), -round(random.uniform(12, 70), 2), "transport", random.choice(transport))
    # advance to next month
    month = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)

statement = pd.DataFrame(sorted(rows, key=lambda r: r["date"]))

# ---- Write outputs -----------------------------------------------------
debts.to_csv(HERE / "debts.csv", index=False)
statement.to_csv(HERE / "bank_statement.csv", index=False)
with pd.ExcelWriter(HERE / "sample_finances.xlsx", engine="openpyxl") as w:
    debts.to_excel(w, sheet_name="Debts", index=False)
    statement.to_excel(w, sheet_name="Statement", index=False)

print(f"debts rows: {len(debts)}")
print(f"statement rows: {len(statement)}")
print("wrote:", [p.name for p in HERE.glob('*') if p.suffix in ('.csv', '.xlsx')])
