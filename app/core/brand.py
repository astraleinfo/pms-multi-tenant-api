"""What the platform calls itself — in one place, so a rename is one edit.

`PRODUCT_NAME` wherever there is room for words: a footer sentence, a PDF's
author and footer line. `PRODUCT_TAGLINE` is for the one slot too small for
it — the tracked capitals under the logo in an email's header, which has to
fit beside the clinic's name on a phone without wrapping.
"""

from __future__ import annotations

PLATFORM_NAME = "Astrale"
PRODUCT_NAME = "Clinic Management System"
PRODUCT_TAGLINE = "CMS"
#: "Astrale Clinic Management System", for a line that names both at once
FULL_NAME = f"{PLATFORM_NAME} {PRODUCT_NAME}"
