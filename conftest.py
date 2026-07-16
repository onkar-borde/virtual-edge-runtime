"""Pytest configuration.

The --hardware flag is what lets the conformance suite run against a real,
physically connected ESP32 rather than only simulations. Off by default, so
CI and contributors without a board still get a clean green run.
"""


def pytest_addoption(parser):
    parser.addoption(
        "--hardware",
        action="store_true",
        default=False,
        help="run the conformance suite against a real connected ESP32 "
             "(requires ver_bridge firmware flashed; will drive real pins)",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "hardware: test drives real, physically connected hardware"
    )
