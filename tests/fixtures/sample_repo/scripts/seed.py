"""Seed script for local development."""

import json
from pathlib import Path

DEFAULT_USERS = 3


class Seeder:
    """Writes fixture users to a JSON file."""

    def __init__(self, target: Path) -> None:
        self.target = target

    @staticmethod
    def build(count: int) -> list[dict]:
        return [{"id": str(i), "email": f"user{i}@example.com"} for i in range(count)]

    def run(self, count: int = DEFAULT_USERS) -> None:
        self.target.write_text(json.dumps(self.build(count)))


def main() -> None:
    Seeder(Path("users.json")).run()
