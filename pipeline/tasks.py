"""任务发现与加载：扫描 tasks/ 下同时含 task_card.json 与 main.py 的目录。"""
import json
import os
from dataclasses import dataclass, field


@dataclass
class Task:
    task_id: str
    dir: str
    card: dict = field(default_factory=dict)

    @property
    def main_py(self):
        return os.path.join(self.dir, "main.py")

    @property
    def reference_dir(self):
        return os.path.join(self.dir, "assets", "reference")


def load_task(task_dir):
    task_dir = os.path.abspath(task_dir)
    with open(os.path.join(task_dir, "task_card.json"), encoding="utf-8") as f:
        card = json.load(f)
    return Task(task_id=card.get("task_id", os.path.basename(task_dir)),
                dir=task_dir, card=card)


def discover_tasks(tasks_dir):
    found = []
    for root, _, files in os.walk(tasks_dir):
        if "task_card.json" in files and "main.py" in files:
            found.append(load_task(root))
    return sorted(found, key=lambda t: t.task_id)
