#!/usr/bin/env python3
"""Legacy entry: relative input/output/features paths resolve from the software root."""
import sys
sys.dont_write_bytecode = True
from ntv3_tsrf.cli import legacy_main

if __name__ == '__main__':
    raise SystemExit(legacy_main())
