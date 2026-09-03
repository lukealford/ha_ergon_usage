"""Quick check: does the parser accept the live accordion structure?"""

import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from app.tariff_rates import extract_tariff_rates

HTML = """
<h2>Your Tariff Info</h2>
<h1>Tariff 11</h1>
<div><p>All usage per kWh</p><span class="font-semibold">$ 0.28895</span>
<span>Supply charge per day</span> <span>$ 1.80508</span></div>
<h1>Tariff 33</h1>
<div><p>All usage per kWh</p> <b>$ 0.16764</b></div>
"""

rates = extract_tariff_rates(HTML, "A-T", datetime.now(timezone.utc))
for rate in rates:
    print(rate.tariff, rate.per_kwh_aud, rate.daily_supply_aud)
