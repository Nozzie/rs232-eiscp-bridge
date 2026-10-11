#!/usr/bin/env bash
#
# Round-trip test for deploy/install.sh and deploy/uninstall.sh.
#
# Verifies that install.sh sets everything up and uninstall.sh reverts it
# exactly -- with no root, and without touching the real system.
#
# Both scripts are copied into a scratch tree with their absolute paths
# rewritten and every privileged command (systemctl/useradd/usermod/userdel/id)
# replaced by an absolute-path stub that keeps its own state, so `useradd` then
# `userdel` and `systemctl enable` then `disable` are modelled faithfully.
#
#   ./tests/test_deploy_scripts.sh
#
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$REPO/.test-install"
FAILURES=0

pass() { printf '  [PASS] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
check() { if [[ $1 == 0 ]]; then pass "$2"; else fail "$2"; fi; }

expect_present() { if [[ -e $1 ]]; then pass "$2"; else fail "$2 (missing: $1)"; fi; }
expect_absent()  { if [[ -e $1 || -L $1 ]]; then fail "$2 (still present: $1)"; else pass "$2"; fi; }

CONF="$T/etc/default/onkyo-bridge"
UNIT="$T/etc/systemd/system/onkyo-bridge.service"
DAEMON="$T/opt/onkyo-bridge/onkyo_serial_bridge.py"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Scratch tree
# ---------------------------------------------------------------------------
rm -rf "$T"
mkdir -p "$T"/{opt,etc/systemd/system,etc,stub,dev/serial/by-id,log,state}

# A fake serial adapter: a symlink in a by-id-style directory.
: > "$T/dev/ttyUSB0"
ln -s "$T/dev/ttyUSB0" "$T/dev/serial/by-id/usb-Fake_Adapter_1234-if00-port0"

# ---------------------------------------------------------------------------
# Stateful stubs
# ---------------------------------------------------------------------------
cat > "$T/stub/systemctl" <<'EOS'
#!/bin/bash
echo "systemctl $*" >> "$STUBLOG"
case "$1" in
  enable)          touch "$STUBSTATE/service-enabled" "$STUBSTATE/service-active" ;;
  disable)         rm -f "$STUBSTATE/service-enabled" "$STUBSTATE/service-active" ;;
  start)           touch "$STUBSTATE/service-active" ;;
  stop)            rm -f "$STUBSTATE/service-active" ;;
  is-active)       [[ -f "$STUBSTATE/service-active" ]] || exit 3 ;;
  is-enabled)      [[ -f "$STUBSTATE/service-enabled" ]] || exit 1 ;;
  list-unit-files) [[ -f "$STUBSTATE/service-enabled" ]] || exit 1 ;;
esac
exit 0
EOS

cat > "$T/stub/useradd" <<'EOS'
#!/bin/bash
echo "useradd $*" >> "$STUBLOG"
touch "$STUBSTATE/user-exists"
exit 0
EOS

cat > "$T/stub/usermod" <<'EOS'
#!/bin/bash
echo "usermod $*" >> "$STUBLOG"
exit 0
EOS

cat > "$T/stub/userdel" <<'EOS'
#!/bin/bash
echo "userdel $*" >> "$STUBLOG"
rm -f "$STUBSTATE/user-exists"
exit 0
EOS

cat > "$T/stub/id" <<'EOS'
#!/bin/bash
if [[ -f "$STUBSTATE/user-exists" ]]; then
    echo "uid=999(onkyo-bridge) gid=999(onkyo-bridge) groups=999(onkyo-bridge),20(dialout)"
    exit 0
fi
exit 1
EOS

chmod +x "$T"/stub/*

# ---------------------------------------------------------------------------
# Rewrite the real scripts into the scratch tree.
#
# A two-stage sed keeps the escaping readable: stage 1 uses @@T@@ as a
# placeholder for the scratch root, stage 2 substitutes it in.
# ---------------------------------------------------------------------------
cat > "$T/rewrite.sed" <<'SED'
s@(^|[[:space:]])systemctl @\1@@T@@/stub/systemctl @g
s@(^|[[:space:]])useradd @\1@@T@@/stub/useradd @g
s@(^|[[:space:]])usermod @\1@@T@@/stub/usermod @g
s@(^|[[:space:]])userdel @\1@@T@@/stub/userdel @g
s@(^|[[:space:]])id "\$USER_NAME" @\1@@T@@/stub/id "$USER_NAME" @g
s@/opt/onkyo-bridge@@@T@@/opt/onkyo-bridge@g
s@/etc/default/onkyo-bridge@@@T@@/etc/default/onkyo-bridge@g
s@/etc/systemd/system@@@T@@/etc/systemd/system@g
s@/dev/serial/by-id@@@T@@/dev/serial/by-id@g
s@/dev/ttyUSB0@@@T@@/dev/ttyUSB0@g
s@\[\[ \$EUID -eq 0 \]\]@true@g
SED
sed "s#@@T@@#$T#g" "$T/rewrite.sed" > "$T/rewrite.final.sed"

rewrite() { sed -E -f "$T/rewrite.final.sed" "$1" > "$2"; }
rewrite "$REPO/deploy/install.sh" "$T/install_test.sh"
rewrite "$REPO/deploy/uninstall.sh" "$T/uninstall_test.sh"

# Harness safety: no privileged command may remain reachable by bare name.
for script in "$T/install_test.sh" "$T/uninstall_test.sh"; do
    for tool in systemctl useradd usermod userdel; do
        if grep -qE "(^|[[:space:]])$tool " "$script"; then
            echo "HARNESS ERROR: $script still calls '$tool' by bare name; refusing to run" >&2
            exit 2
        fi
    done
done

run_script() {
    PATH="$T/stub:$PATH" STUBLOG="$T/log/stub.log" STUBSTATE="$T/state" \
        bash "$1" > "$2" 2>&1
    echo $?
}

# ---------------------------------------------------------------------------
log "PHASE 1 - install.sh"
RC=$(run_script "$T/install_test.sh" "$T/log/install.log")
sed 's/^/    /' "$T/log/install.log" | head -12

check $([[ $RC -eq 0 ]] && echo 0 || echo 1) "install.sh exits 0 (got $RC)"
expect_present "$DAEMON" "daemon installed"
expect_present "$UNIT" "unit installed"
expect_present "$CONF" "config written"

if grep -qE "ONKYO_DEVICE=$T/dev/serial/by-id/usb-Fake_Adapter_1234-if00-port0" "$CONF" 2>/dev/null; then
    pass "serial device detected and substituted"
else
    fail "serial device substitution wrong: $(grep ONKYO_DEVICE "$CONF" 2>/dev/null)"
fi
if grep -qE '^ONKYO_IDENTIFIER=[0-9A-F]{1,12}$' "$CONF" 2>/dev/null; then
    pass "identifier is a valid MAC-style string"
else
    fail "identifier malformed"
fi
if grep -qi "invalid option" "$T/log/install.log"; then
    fail "option error in install output"
else
    pass "no command-line option errors"
fi
if [[ -f $T/state/service-active ]]; then
    pass "service is marked active after install"
else
    fail "service not started by install"
fi

# ---------------------------------------------------------------------------
log ""
log "PHASE 2 - uninstall.sh --dry-run must not change anything"

PATH="$T/stub:$PATH" STUBLOG="$T/log/stub.log" STUBSTATE="$T/state" \
    bash "$T/uninstall_test.sh" --dry-run > "$T/log/dryrun.log" 2>&1
RC=$?
sed 's/^/    /' "$T/log/dryrun.log" | head -8

check $([[ $RC -eq 0 ]] && echo 0 || echo 1) "dry run exits 0 (got $RC)"
expect_present "$UNIT" "dry run left the unit in place"
expect_present "$DAEMON" "dry run left /opt/onkyo-bridge in place"
expect_present "$CONF" "dry run left the config in place"
if [[ -f $T/state/user-exists ]]; then
    pass "dry run left the system user in place"
else
    fail "dry run deleted the system user"
fi

# ---------------------------------------------------------------------------
log ""
log "PHASE 3 - uninstall.sh (full)"

RC=$(run_script "$T/uninstall_test.sh" "$T/log/uninstall.log")
sed 's/^/    /' "$T/log/uninstall.log" | head -20

check $([[ $RC -eq 0 ]] && echo 0 || echo 1) "uninstall.sh exits 0 (got $RC)"
expect_absent "$UNIT" "unit file removed"
expect_absent "$DAEMON" "program directory removed"
expect_absent "$CONF" "config removed"
if [[ -f $T/state/user-exists ]]; then
    fail "system user still present after uninstall"
else
    pass "system user removed"
fi
if [[ -f $T/state/service-active ]]; then
    fail "service still marked active"
else
    pass "service stopped"
fi
if grep -qE "userdel onkyo-bridge" "$T/log/stub.log"; then
    pass "userdel was actually invoked"
else
    fail "userdel never invoked"
fi
if grep -qE "systemctl disable" "$T/log/stub.log"; then
    pass "systemctl disable was actually invoked"
else
    fail "systemctl disable never invoked"
fi

# ---------------------------------------------------------------------------
log ""
log "PHASE 4 - uninstall.sh is idempotent"

RC=$(run_script "$T/uninstall_test.sh" "$T/log/uninstall2.log")
check $([[ $RC -eq 0 ]] && echo 0 || echo 1) "second uninstall exits 0 (got $RC)"
expect_absent "$UNIT" "unit still absent"
expect_absent "$DAEMON" "program dir still absent"

# ---------------------------------------------------------------------------
log ""
log "PHASE 5 - --keep-user / --keep-config honour their flags"

RC=$(run_script "$T/install_test.sh" "$T/log/install2.log")
check $([[ $RC -eq 0 ]] && echo 0 || echo 1) "reinstall exits 0 (got $RC)"

PATH="$T/stub:$PATH" STUBLOG="$T/log/stub.log" STUBSTATE="$T/state" \
    bash "$T/uninstall_test.sh" --keep-user --keep-config > "$T/log/keep.log" 2>&1
RC=$?
check $([[ $RC -eq 0 ]] && echo 0 || echo 1) "--keep-* uninstall exits 0 (got $RC)"
expect_absent "$UNIT" "unit removed with --keep-*"
expect_present "$CONF" "--keep-config preserved the config"
if [[ -f $T/state/user-exists ]]; then
    pass "--keep-user preserved the system user"
else
    fail "--keep-user deleted the system user anyway"
fi
if grep -qi "kept" "$T/log/keep.log"; then
    pass "--keep-* reported what it kept"
else
    fail "--keep-* did not report kept items"
fi

# ---------------------------------------------------------------------------
log ""
log "PHASE 6 - final cleanup restores a pristine tree"

RC=$(run_script "$T/uninstall_test.sh" "$T/log/final.log")
check $([[ $RC -eq 0 ]] && echo 0 || echo 1) "final uninstall exits 0 (got $RC)"
expect_absent "$CONF" "config removed in final pass"
if [[ -f $T/state/user-exists ]]; then
    fail "user survived the final pass"
else
    pass "user removed in final pass"
fi

echo
if [[ $FAILURES -eq 0 ]]; then
    echo "ALL DEPLOY-SCRIPT CHECKS PASSED"
else
    echo "$FAILURES DEPLOY-SCRIPT CHECK(S) FAILED"
fi
exit $((FAILURES > 0))
