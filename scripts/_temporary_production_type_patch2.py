from __future__ import annotations

import runpy
from pathlib import Path

runpy.run_path("scripts/_temporary_production_type_patch.py", run_name="__main__")

services = Path("src/firmquant/application/production_services.py")
text = services.read_text(encoding="utf-8")
old = "self._universe.base_symbols"
if text.count(old) != 1:
    raise RuntimeError(f"deployment universe anchor count: {text.count(old)}")
text = text.replace(old, "self._universe.deployment_symbols", 1)
services.write_text(text, encoding="utf-8", newline="\n")

daemon = Path("src/firmquant/application/production_daemon.py")
text = daemon.read_text(encoding="utf-8")
old = "    def sink(self) -> object: ...\n"
new = "    def sink(self) -> BrokerEventSink: ...\n"
if text.count(old) != 1:
    raise RuntimeError(f"event pump sink anchor count: {text.count(old)}")
text = text.replace(old, new, 1)
daemon.write_text(text, encoding="utf-8", newline="\n")
