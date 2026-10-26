# ghosthand-client

**Zero-dependency** Python client for the `ghosthand ui` console, handy in pytest or CI.
Xcode, the runner and the model stay on the Mac running the console.

```bash
pip install ./packages/ghosthand-client
```

```python
from ghosthand_client import GhosthandClient

client = GhosthandClient("http://localhost:8000")

def test_dark_mode():
    result = client.run("Turn on dark mode in Settings", mode="reliable", device="iPhone 17")
    assert result.succeeded, result.summary
```

- `run()` waits for the task to finish.
- `start()` / `status()` / `cancel()` to drive a task without waiting.
- `devices()`, `screenshot()`, `elements()`, `tap()`, `launch()` for direct checks.
- Errors raise `GhosthandError`, with the HTTP status in `.status`.
