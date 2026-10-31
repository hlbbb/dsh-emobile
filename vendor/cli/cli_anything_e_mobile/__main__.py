"""python -m cli_anything_e_mobile 入口。"""

from .cli import _force_utf8_output, cli


def main() -> None:
    _force_utf8_output()
    cli(prog_name="cli-anything-e-mobile")


if __name__ == "__main__":
    main()
