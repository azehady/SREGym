#!/usr/bin/env bash
# fault_sim's own dev server. Pins its worktree and its own ports so it never contends
# with the eval-ui session on :8501/:3000. Idempotent.
#
#   fault-sim-up.sh            start (or report already-up)
#   fault-sim-up.sh stop       stop
#   fault-sim-up.sh status     what is serving, and from which checkout
set -u

export HOME=/workspace/home
WT=/workspace/home/ciroos/_wt-faultsim
EXPOSE=${FAULT_SIM_PORT:-8503}        # tailnet-facing; 8501=eval-ui, 8502=streamlit-rca
INTERNAL=${FAULT_SIM_DEV_PORT:-3103}  # rspack bind; 3000 belongs to the other session
LOG=$HOME/.dashboard-logs/fault-sim.log
# Our OWN compile log: observer-dev-eval.sh truncates DEV_LOG, and its default is shared with eval-ui.
export DEV_LOG=${DEV_LOG:-$HOME/.dashboard-logs/fault-sim-compile.log}

alive() { ss -ltn 2>/dev/null | grep -q ":$EXPOSE "; }

mine() {  # pids of rspack serving OUR worktree
  for p in $(pgrep -f rspack 2>/dev/null); do
    case "$(readlink /proc/$p/cwd 2>/dev/null)" in *_wt-faultsim*) echo "$p";; esac
  done
}

case "${1:-start}" in
  start)
    if alive; then echo "fault_sim: already up on :$EXPOSE"; exit 0; fi
    [ -d "$WT/observer/node_modules" ] && [ ! -L "$WT/observer/node_modules" ] \
      || { echo "fault_sim: $WT needs its OWN node_modules (a symlink breaks on @openfeature/*)"; exit 1; }
    EXPOSE_PORT="$EXPOSE" DEV_PORT="$INTERNAL" DEV_ALLOW_TUNNEL_HOSTS=true EVAL_WT="$WT" \
      setsid bash "$HOME/bin/observer-dev-eval.sh" </dev/null >>"$LOG" 2>&1 &
    for _ in $(seq 1 60); do alive && break; sleep 5; done
    alive && echo "fault_sim: up on :$EXPOSE (internal :$INTERNAL), serving $WT" \
           || { echo "fault_sim: FAILED to bind :$EXPOSE — see $LOG"; tail -5 "$LOG"; exit 1; }
    ;;
  stop)
    for p in $(mine); do kill "$p" 2>/dev/null && echo "  stopped rspack $p"; done
    for p in $(pgrep -f "forwarder.py --listen $EXPOSE" 2>/dev/null); do kill "$p" 2>/dev/null; done
    echo "fault_sim: stopped"
    ;;
  status)
    alive && echo "  :$EXPOSE UP" || echo "  :$EXPOSE down"
    for p in $(mine); do echo "  rspack $p cwd=$(readlink /proc/$p/cwd)"; done
    echo "  branch: $(git -C "$WT" rev-parse --abbrev-ref HEAD 2>/dev/null)"
    ;;
  *) echo "usage: fault-sim-up.sh {start|stop|status}"; exit 2;;
esac
