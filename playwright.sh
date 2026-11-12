#!/bin/bash
export LD_LIBRARY_PATH="/tmp/qb-deps/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH"
cd /tmp/playwright-test && node "$@"
