import threading
from dataclasses import dataclass, field

from .agent import Agent, RunResult, Step, new_run_id
from .device import Device
from .llm import Claude
from .planning import Checkpoint


@dataclass
class Task:
    run_id: str
    task: str
    device: str
    udid: str = ""
    mode: str = "fast"
    thread: threading.Thread | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    steps: list[Step] = field(default_factory=list)
    plan: list[Checkpoint] = field(default_factory=list)
    result: RunResult | None = None
    error: str = ""

    @property
    def status(self) -> str:
        if self.result:
            return self.result.status
        if self.error:
            return "error"
        return "cancelling" if self.cancel.is_set() else "running"

    @property
    def active(self) -> bool:
        return self.result is None and not self.error


class TaskManager:
    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    def start(self, task: str, device: Device, max_steps: int = 30, model: str | None = None,
              mode: str = "fast") -> Task:
        llm = Claude(model) if model else Claude()
        entry = Task(run_id=new_run_id(), task=task, device=device.target.name, udid=device.target.udid, mode=mode)

        def on_plan(plan: list[Checkpoint], notes: str) -> None:
            entry.plan = plan

        agent = Agent(device, llm, max_steps=max_steps, on_step=entry.steps.append, cancel=entry.cancel,
                      mode=mode, on_plan=on_plan)

        def work() -> None:
            try:
                entry.result = agent.run(task, run_id=entry.run_id)
            except Exception as error:
                entry.error = f"{type(error).__name__}: {error}"

        entry.thread = threading.Thread(target=work, name=f"ghosthand-{entry.run_id}", daemon=True)
        with self._lock:
            # One run per device: two agents tapping the same screen would fight.
            for other in self._tasks.values():
                if other.udid == entry.udid and other.active:
                    raise RuntimeError(f"task {other.run_id} is already running on {entry.device}")
            self._tasks[entry.run_id] = entry
        entry.thread.start()
        return entry

    def get(self, run_id: str) -> Task | None:
        return self._tasks.get(run_id)

    def all(self) -> list[Task]:
        return list(self._tasks.values())
