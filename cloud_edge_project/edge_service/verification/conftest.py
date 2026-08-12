from __future__ import annotations

import sys
import importlib.util
from pathlib import Path
from types import ModuleType


if importlib.util.find_spec("paho") is None:
    paho_module = ModuleType("paho")
    mqtt_module = ModuleType("paho.mqtt")
    client_module = ModuleType("paho.mqtt.client")

    class _CallbackAPIVersion:
        VERSION2 = 2

    class _Protocol:
        pass

    client_module.CallbackAPIVersion = _CallbackAPIVersion
    client_module.MQTTv311 = _Protocol()
    client_module.MQTT_ERR_SUCCESS = 0
    client_module.Client = None
    paho_module.mqtt = mqtt_module
    mqtt_module.client = client_module
    sys.modules.update(
        {
            "paho": paho_module,
            "paho.mqtt": mqtt_module,
            "paho.mqtt.client": client_module,
        }
    )


PROJECT = Path(__file__).resolve().parents[2]
for path in (PROJECT, PROJECT / "edge_service" / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
