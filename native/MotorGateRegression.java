package io.daxqo.app;

/** Runs with a normal JDK, independently of Android and the model. */
public final class MotorGateRegression {
    private static void check(boolean result, String message) { if (!result) throw new AssertionError(message); }
    public static void main(String[] args) throws Exception {
        long[] now = {100}; MotorGate gate = new MotorGate(() -> now[0]);
        check(gate.acquire("scout", "io.daxqo.fixture", 1000, "").isEmpty(), "acquire");
        String token = gate.lease(); long generation = gate.generation();
        check(gate.check("", "", 0, "io.daxqo.fixture").equals("motor_busy"), "legacy excluded");
        check(gate.check(token, "scout", generation, "io.daxqo.fixture").isEmpty(), "holder");
        check(gate.acquire("reflex", "io.daxqo.fixture", 1000, "").equals("motor_busy"), "second owner");
        check(gate.acquire("scout", "io.daxqo.fixture", 1000, token).isEmpty(), "renew");
        check(!gate.release("wrong"), "release identity");
        gate.revoke("manual_touch");
        check(gate.check(token, "scout", generation, "io.daxqo.fixture").equals("motor_cancelled"), "cancel fence");
        check(gate.acquire("scout", "io.daxqo.fixture", 1000, token).equals("motor_cancelled"), "renew cannot resurrect cancelled owner");
        check(gate.acquire("reflex", "io.daxqo.fixture", 1000, "").isEmpty(), "reacquire");
        check(!gate.cancel(token), "old owner cannot cancel current lease");
        check(!gate.snapshot().lease.isEmpty() && gate.snapshot().owner.equals("reflex"), "atomic lease snapshot");
        check(gate.check(token, "scout", generation, "io.daxqo.fixture").equals("motor_busy"), "old generation");
        String second = gate.lease(); long gen2 = gate.generation(); now[0] += 1100;
        check(gate.check(second, "reflex", gen2, "io.daxqo.fixture").equals("motor_cancelled"), "expiry");
        check(gate.acquire("reflex", "io.daxqo.fixture", 1000, "").isEmpty(), "new lease");
        check(gate.check(gate.lease(), "reflex", gate.generation(), "other.app").equals("foreground_changed"), "package fence");
        MotorEvents events = new MotorEvents();
        Thread emitter = new Thread(() -> events.changed(4096, "io.daxqo.fixture", 0, 25, 1, 5, 30));
        emitter.start(); check(events.await(0, 500) == 1, "event wake"); emitter.join();
        check(events.scrollY() == 25 && events.itemCount() == 30, "scroll data");
        check(events.await(1, 1) == 1, "bounded idle wait");
        System.out.println("Motor native gate/event regressions passed");
    }
}
