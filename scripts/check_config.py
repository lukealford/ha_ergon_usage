"""Validate config.yaml against HA Supervisor's add-on requirements."""

import re
import yaml

d = yaml.safe_load(open("config.yaml", encoding="utf-8-sig"))

checks = {
    "name is str": isinstance(d.get("name"), str),
    "slug matches ^[a-z0-9_]+$": bool(re.match(r"^[a-z0-9_]+$", d.get("slug", ""))),
    "description is str": isinstance(d.get("description"), str),
    "version is str": isinstance(d.get("version"), str),
    "arch is list of str": isinstance(d.get("arch"), list)
    and all(isinstance(a, str) for a in d["arch"]),
    "startup valid": d.get("startup") in ("initialize", "system", "services", "application", "once"),
    "boot valid": d.get("boot") in ("auto", "manual"),
    "schema is dict": isinstance(d.get("schema"), dict),
    "options is dict": isinstance(d.get("options"), dict),
    "no image key (build.yaml used)": "image" not in d,
    "url https": (d.get("url") or "").startswith("https://"),
}
for key, ok in checks.items():
    print(("PASS" if ok else "FAIL"), key)
