import argparse
import sys

from .core import validate_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="iso20022-validate",
        description="Validate a pain.001 message (schema version detected from the namespace) against the ISO 20022 XSD schema.",
    )
    parser.add_argument("file", help="XML file to validate")
    args = parser.parse_args(argv)

    result = validate_file(args.file)
    print(result.format())
    return 0 if result.valid else 1


if __name__ == "__main__":
    sys.exit(main())
