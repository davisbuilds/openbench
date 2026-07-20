#!/usr/bin/env bash
# Fixture checker that sleeps well past any tiny --checker-timeout, so the
# runner must abort it and record checker_exit="timeout", success=false.
sleep 30
exit 0
