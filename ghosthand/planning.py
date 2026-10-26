from dataclasses import asdict, dataclass

from .llm import DecisionMaker, Usage

PLAN_SYSTEM = """\
You plan how an automation agent will complete a task on an iOS device. You see the task, the installed \
apps and the current screen.

Write 2 to 6 checkpoints, in order. Each checkpoint is an intermediate state the agent must reach, with the \
evidence that will be visible on screen once it is reached (a title, a toggle state, a field value). The last \
checkpoint is the task's outcome. Plan states, not taps: the agent decides the taps. If the task cannot be \
done on this device (missing app, needs data you don't have), still plan, and say so in the notes.
"""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "checkpoints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "State to reach, e.g. 'Display settings open'."},
                    "evidence": {"type": "string", "description": "What on screen proves it."},
                },
                "required": ["goal", "evidence"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string", "description": "Risks or blockers worth knowing; empty if none."},
    },
    "required": ["checkpoints", "notes"],
    "additionalProperties": False,
}

CHECK_SYSTEM = """\
You verify the work of an automation agent on an iOS device. You did not take the actions; judge only from \
the current screen (element list and screenshot) and the agent's journal. Be strict: a claim is verified \
only if the evidence is visible now. Text the agent typed into a field that was not submitted, or a screen \
that merely looks similar, does not count.
"""

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "verified": {"type": "boolean"},
        "reason": {"type": "string", "description": "One sentence citing what is (or is not) on screen."},
    },
    "required": ["verified", "reason"],
    "additionalProperties": False,
}


@dataclass
class Checkpoint:
    goal: str
    evidence: str
    done: bool = False


@dataclass
class Check:
    verified: bool
    reason: str
    usage: Usage


def make_plan(llm: DecisionMaker, task: str, apps: str, screen: str,
              screenshot: bytes | None) -> tuple[list[Checkpoint], str, Usage]:
    prompt = f"<task>\n{task}\n</task>\n\n{apps}\n\n<screen>\n{screen}\n</screen>"
    judgment = llm.judge([PLAN_SYSTEM], prompt, screenshot, PLAN_SCHEMA, effort="medium")
    if not judgment.data or not judgment.data.get("checkpoints"):
        # Planning is a help, not a requirement: fall back to the task itself as the only checkpoint.
        return [Checkpoint(task, "The task's outcome is visible on screen.")], judgment.error, judgment.usage
    checkpoints = [Checkpoint(str(c["goal"]), str(c["evidence"])) for c in judgment.data["checkpoints"]][:6]
    return checkpoints, str(judgment.data.get("notes", "")), judgment.usage


def render_plan(checkpoints: list[Checkpoint]) -> str:
    current = next((i for i, c in enumerate(checkpoints) if not c.done), None)
    lines = []
    for i, checkpoint in enumerate(checkpoints):
        mark = "x" if checkpoint.done else (">" if i == current else " ")
        lines.append(f"[{mark}] {i}. {checkpoint.goal} (evidence: {checkpoint.evidence})")
    return "\n".join(lines)


def verify(llm: DecisionMaker, task: str, claim: str, journal: list[str], screen: str,
           screenshot: bytes | None) -> Check:
    history = "\n".join(journal[-20:]) or "(no steps yet)"
    prompt = (f"<task>\n{task}\n</task>\n\n<claim>\n{claim}\n</claim>\n\n<journal>\n{history}\n</journal>\n\n"
              f"<screen>\n{screen}\n</screen>\n\nIs the claim verified by the current screen?")
    judgment = llm.judge([CHECK_SYSTEM], prompt, screenshot, CHECK_SCHEMA, effort="low")
    if not judgment.data:
        return Check(False, f"verification unavailable: {judgment.error}", judgment.usage)
    return Check(bool(judgment.data["verified"]), str(judgment.data["reason"]), judgment.usage)


def plan_to_dicts(checkpoints: list[Checkpoint]) -> list[dict]:
    return [asdict(c) for c in checkpoints]
