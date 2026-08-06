#!/usr/bin/env python
"""Django's command-line utility."""

from __future__ import annotations

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django is not importable. It must be installed into the same interpreter "
            "that has ecg-pipeline -- see README, 'Environment'."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
