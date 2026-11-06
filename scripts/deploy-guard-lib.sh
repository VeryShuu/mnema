#!/usr/bin/env bash
# Politika blokirovki deploya po rezultatu proveryki aktivnykh zapisey.
#
# Neizvestnoe sostojanie (API ne otvechal / otvet ne razobran / net parserta)
# blokiruet deploj na ravnih s aktivnymi zapisjami: imenno pri upavshem API
# vseh bolee veroyatna zhivaya vstrecha, kotoryu `--force-recreate web-bot`
# ubyet. Escape-hatch edinyy: FORCE_DEPLOY=1 prodolzhayet pri lyubom rc.

# guard_should_stop <rc> <force>
#   rc    — kod vykhoda proveryki: 0 net aktivnykh, 1 est', >1 neizvestno
#   force — "1" esli FORCE_DEPLOY=1
# Vozvrat: 0 — ostanovit' deploj, 1 — prodolzhit'.
guard_should_stop() {
    local rc="$1" force="$2"
    [ "$rc" = "0" ] && return 1
    [ "$force" = "1" ] && return 1
    return 0
}
