package io.daxqo.app;

/** Bounded event-driven observation; never stores UI text. */
final class MotorEvents {
    private long sequence;
    private int type, scrollX = -1, scrollY = -1, fromIndex = -1, toIndex = -1, itemCount = -1;
    private String packageName = "";
    synchronized void changed(int kind, String pkg, int x, int y, int from, int to, int count) {
        sequence++; type = kind; packageName = pkg;
        if (kind == 4096) { scrollX = x; scrollY = y; fromIndex = from; toIndex = to; itemCount = count; }
        notifyAll();
    }
    synchronized long sequence() { return sequence; }
    synchronized long await(long after, long timeout) throws InterruptedException {
        long deadline = System.nanoTime() + Math.min(2000, Math.max(0, timeout)) * 1000000L;
        while (sequence <= after) {
            long remaining = deadline - System.nanoTime();
            if (remaining <= 0) break;
            wait(Math.max(1, remaining / 1000000L));
        }
        return sequence;
    }
    synchronized int type() { return type; }
    synchronized String packageName() { return packageName; }
    synchronized int scrollX() { return scrollX; }
    synchronized int scrollY() { return scrollY; }
    synchronized int fromIndex() { return fromIndex; }
    synchronized int toIndex() { return toIndex; }
    synchronized int itemCount() { return itemCount; }
}
