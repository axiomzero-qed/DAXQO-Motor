package io.daxqo.app;

import java.util.UUID;
import java.util.function.LongSupplier;

/** Device-wide ownership. No UI, credentials or model state enter this gate. */
final class MotorGate {
    static final class Snapshot {
        final String lease, owner, expectedPackage, reason;
        final long generation, remaining;
        Snapshot(String lease, String owner, String expectedPackage, String reason, long generation, long remaining) {
            this.lease = lease; this.owner = owner; this.expectedPackage = expectedPackage; this.reason = reason;
            this.generation = generation; this.remaining = remaining;
        }
    }
    private final LongSupplier clock;
    private String lease = "", owner = "", expectedPackage = "", reason = "";
    private long generation = 0, expires = 0;

    MotorGate(LongSupplier clock) { this.clock = clock; }
    synchronized void expire() {
        if (!lease.isEmpty() && clock.getAsLong() >= expires) revoke("lease_expired");
    }
    synchronized String acquire(String requestedOwner, String expected, long ttl, String renewing) {
        expire();
        if (requestedOwner == null || !requestedOwner.matches("[A-Za-z0-9_.:-]{1,128}")
                || expected == null || !expected.matches("[A-Za-z0-9_]+(\\.[A-Za-z0-9_]+)+")
                || expected.length() > 200 || ttl < 100 || ttl > 120000) return "invalid_args";
        if (!lease.isEmpty()) {
            if (!lease.equals(renewing) || !owner.equals(requestedOwner) || !expectedPackage.equals(expected)) return "motor_busy";
        } else {
            if (renewing != null && !renewing.isEmpty()) return "motor_cancelled";
            lease = UUID.randomUUID().toString(); owner = requestedOwner;
            expectedPackage = expected; generation++; reason = "";
        }
        expires = clock.getAsLong() + ttl;
        return "";
    }
    synchronized String check(String suppliedLease, String suppliedOwner, long suppliedGeneration, String actualPackage) {
        expire();
        boolean supplied = suppliedLease != null && !suppliedLease.isEmpty();
        if (lease.isEmpty()) return supplied ? "motor_cancelled" : "";
        if (!lease.equals(suppliedLease) || !owner.equals(suppliedOwner) || generation != suppliedGeneration) return "motor_busy";
        if (!expectedPackage.equals(actualPackage)) { revoke("foreground_changed"); return "foreground_changed"; }
        return "";
    }
    synchronized boolean release(String suppliedLease) {
        expire();
        if (lease.isEmpty() || !lease.equals(suppliedLease)) return false;
        revoke("released"); return true;
    }
    synchronized void revoke(String why) {
        generation++; lease = ""; owner = ""; expectedPackage = ""; expires = 0;
        reason = why == null ? "cancelled" : why;
    }
    synchronized boolean cancel(String suppliedLease) {
        expire();
        if (suppliedLease != null && !suppliedLease.isEmpty() && !lease.equals(suppliedLease)) return false;
        revoke("task_cancelled"); return true;
    }
    synchronized Snapshot snapshot() {
        expire();
        return new Snapshot(lease, owner, expectedPackage, reason, generation, Math.max(0, expires - clock.getAsLong()));
    }
    synchronized String lease() { expire(); return lease; }
    synchronized String owner() { expire(); return owner; }
    synchronized String expectedPackage() { expire(); return expectedPackage; }
    synchronized String reason() { expire(); return reason; }
    synchronized long generation() { expire(); return generation; }
    synchronized long remaining() { expire(); return Math.max(0, expires - clock.getAsLong()); }
}
