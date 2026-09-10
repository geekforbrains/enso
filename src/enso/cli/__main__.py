"""``python -m enso.cli``: the module form of the ``enso`` command.

The installed ``enso`` console script calls ``enso.cli:main`` directly, so this file
exists only so the package can also be run with ``python -m`` from a checkout.
"""

from . import main

main()
