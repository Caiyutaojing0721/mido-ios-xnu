#!/usr/bin/env python3
"""Compatibility shim -- superseded by tools/lse_scan.py.

This script used to walk only `__TEXT_EXEC` and to allow-list the atomics
inside libfirehose_kernel as "never reached during early boot".  Both parts
were wrong:

  * `__TEXT_EXEC` is not the only section that holds instructions, and
  * those six firehose atomics DO run on the boot path -- the kernel trapped
    at `__firehose_buffer_tracepoint_flush+0x18` on `ldadd x8, x8, [x0]`
    (0xf8280128) as soon as it got past zone bootstrap.

Nothing is allow-listed any more: libfirehose_kernel is now built for plain
arm64 with `-mcpu=cortex-a53+crypto` (see .github/workflows/xnu-mido.yml), so
the kernel must contain zero ARMv8.1 atomic words.  Kept only so that older
instructions/docs calling this path still get the strict answer.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lse_scan  # noqa: E402

if __name__ == '__main__':
    print('check_lse.py is a shim for lse_scan.py (zero tolerance)')
    sys.exit(lse_scan.main())
