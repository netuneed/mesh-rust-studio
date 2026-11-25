"""Keep legacy add-ons on the pkg_resources snapshot distributed by the pinned pip."""

import sys

from pip._vendor import pkg_resources

sys.modules[__name__] = pkg_resources
