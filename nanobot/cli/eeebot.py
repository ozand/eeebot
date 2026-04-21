"""Compatibility CLI alias for the eeebot public project name."""

from eeebot import __logo__
from eeebot.cli.commands import app


app.info.name = "eeebot"
app.info.help = f"{__logo__} eeebot - eeepc self-improving runtime"


def main() -> None:
    app()


if __name__ == "__main__":
    main()
