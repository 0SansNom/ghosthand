from ghosthand.simctl import parse_devices


def test_parse_devices_names_runtimes_and_skips_unavailable():
    payload = {
        "devices": {
            "com.apple.CoreSimulator.SimRuntime.iOS-27-1": [
                {"udid": "A", "name": "iPhone 17", "state": "Booted", "isAvailable": True},
                {"udid": "B", "name": "iPhone Air", "state": "Shutdown", "isAvailable": False},
            ],
            "com.apple.CoreSimulator.SimRuntime.iOS-27-0": [
                {"udid": "C", "name": "iPhone 17", "state": "Shutdown", "isAvailable": True},
            ],
        }
    }
    sims = parse_devices(payload)
    assert [(s.udid, s.runtime, s.booted) for s in sims] == [("A", "iOS 27.1", True), ("C", "iOS 27.0", False)]
