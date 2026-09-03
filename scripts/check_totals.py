"""Cross-check probe.json totals against the portal's reported total."""

import json

data = json.load(open("probe.json"))
readings = data["readings"]
t11 = [r for r in readings if r["tariff"] == "RTC11"]
t33 = [r for r in readings if r["tariff"] == "RTC33"]
t11_total = sum(float(r["kwh"]) for r in t11)
t33_total = sum(float(r["kwh"]) for r in t33)
hours = len({r["timestamp_brisbane"] for r in readings})
print(f"Tariff 11: {len(t11)} readings, total {t11_total:.3f} kWh")
print(f"Tariff 33: {len(t33)} readings, total {t33_total:.3f} kWh")
print(f"Combined: {t11_total + t33_total:.3f} kWh (portal reported 47.79)")
print(f"Distinct hours: {hours}")
