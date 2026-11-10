"""Keep GPUs on Modal warm for a test session, and let them go afterwards.

    python manage.py gpu_warm 10     # before the session: ten GPUs up and waiting
    python manage.py gpu_warm 0      # after it: back to starting on demand

A cold container takes one to two minutes before its first study; a warm one answers in
about 17 seconds. Ten warm T4s cost about 0.76 USD an hour each while they wait, so this
is for the hour of a session, never left on. Inside the Docker setup:

    docker compose exec worker python manage.py gpu_warm 10
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from analysis.modal_runner import remote_analyzer


class Command(BaseCommand):
    help = "Set how many GPU containers stay warm on Modal (0 turns warming off)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("containers", type=int, help="GPU containers to keep up (0 to 10).")

    def handle(self, *args: Any, **options: Any) -> None:
        containers = options["containers"]
        if not 0 <= containers <= 10:
            raise CommandError("Between 0 and 10: the Modal Starter plan allows ten GPUs at once.")
        remote_analyzer().update_autoscaler(min_containers=containers)
        if containers:
            self.stdout.write(self.style.SUCCESS(f"{containers} GPU(s) starting; ready in one to two minutes."))
        else:
            self.stdout.write(self.style.SUCCESS("Warming off: GPUs shut down after five idle minutes."))
