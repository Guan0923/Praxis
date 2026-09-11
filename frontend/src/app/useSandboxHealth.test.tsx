import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  getSandboxStatus,
  repairSandboxBroker,
  type SandboxBrokerStatus,
} from "../api";
import { useSandboxHealth, type SandboxHealthState } from "./useSandboxHealth";

vi.mock("../api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../api")>(),
  getSandboxStatus: vi.fn(),
  repairSandboxBroker: vi.fn(),
}));

const START = new Date("2026-09-11T00:00:00Z");
let current: SandboxHealthState;

function Harness() {
  current = useSandboxHealth();
  return <output>{current.phase}</output>;
}

function status(healthy: boolean): SandboxBrokerStatus {
  return healthy
    ? { installed: true, healthy: true }
    : { installed: true, healthy: false, code: "broker_pipe_unavailable", detail: "stopped" };
}

async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function advance(milliseconds: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(milliseconds);
  });
  await settle();
}

describe("useSandboxHealth", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(START);
    vi.mocked(getSandboxStatus).mockReset();
    vi.mocked(repairSandboxBroker).mockReset();
    vi.mocked(repairSandboxBroker).mockResolvedValue(status(true));
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("checks immediately and polls a healthy broker every 30 seconds", async () => {
    vi.mocked(getSandboxStatus).mockResolvedValue(status(true));
    render(<Harness />);
    await settle();

    expect(current.phase).toBe("healthy");
    expect(current.autoRecoveryPhase).toBe("idle");
    expect(repairSandboxBroker).not.toHaveBeenCalled();
    act(() => current.notifyUserBackendRequest());
    await settle();
    expect(getSandboxStatus).toHaveBeenCalledTimes(1);
    await advance(29_999);
    expect(getSandboxStatus).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(getSandboxStatus).toHaveBeenCalledTimes(2);
  });

  it("requires ten continuous unhealthy seconds before repairing", async () => {
    vi.mocked(getSandboxStatus).mockResolvedValue(status(false));
    render(<Harness />);
    await settle();

    expect(current.autoRecoveryPhase).toBe("observing");
    expect(current.nextRetryAt).toBe(START.getTime() + 10_000);
    await advance(9_999);
    expect(repairSandboxBroker).not.toHaveBeenCalled();

    await advance(1);
    expect(repairSandboxBroker).toHaveBeenCalledTimes(1);
    expect(current.autoRecoveryPhase).toBe("verifying");
    expect(current.nextRetryAt).toBe(START.getTime() + 20_000);
  });

  it("cancels automatic repair as soon as an observing check becomes healthy", async () => {
    vi.mocked(getSandboxStatus).mockImplementation(async () => status(Date.now() >= START.getTime() + 3_000));
    render(<Harness />);
    await settle();

    await advance(3_000);
    expect(current.phase).toBe("healthy");
    expect(current.autoRecoveryPhase).toBe("idle");
    expect(current.nextRetryAt).toBeNull();
    expect(repairSandboxBroker).not.toHaveBeenCalled();
  });

  it("checks every second after repair and stops when the broker becomes healthy", async () => {
    vi.mocked(getSandboxStatus).mockImplementation(async () => status(Date.now() >= START.getTime() + 11_000));
    render(<Harness />);
    await settle();

    await advance(10_000);
    expect(repairSandboxBroker).toHaveBeenCalledTimes(1);
    expect(current.autoRecoveryPhase).toBe("verifying");

    await advance(1_000);
    expect(current.phase).toBe("healthy");
    expect(current.autoRecoveryPhase).toBe("idle");
    expect(current.nextRetryAt).toBeNull();
  });

  it("repairs again immediately after ten unhealthy verification seconds", async () => {
    vi.mocked(getSandboxStatus).mockResolvedValue(status(false));
    render(<Harness />);
    await settle();

    await advance(19_999);
    expect(repairSandboxBroker).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(repairSandboxBroker).toHaveBeenCalledTimes(2);
    expect(current.autoRecoveryPhase).toBe("verifying");
    expect(current.nextRetryAt).toBe(START.getTime() + 30_000);
  });

  it("user requests check immediately without resetting the recovery deadline", async () => {
    vi.mocked(getSandboxStatus).mockResolvedValue(status(false));
    render(<Harness />);
    await settle();
    const deadline = current.nextRetryAt;

    await advance(5_000);
    act(() => current.notifyUserBackendRequest());
    await settle();

    expect(getSandboxStatus).toHaveBeenCalledTimes(7);
    expect(current.nextRetryAt).toBe(deadline);
    expect(repairSandboxBroker).not.toHaveBeenCalled();
    await advance(5_000);
    expect(repairSandboxBroker).toHaveBeenCalledTimes(1);
  });

  it("manual repair runs immediately and uses the same verification window", async () => {
    vi.mocked(getSandboxStatus).mockResolvedValue(status(true));
    render(<Harness />);
    await settle();

    await act(async () => current.repairManually());
    expect(repairSandboxBroker).toHaveBeenCalledTimes(1);
    expect(current.manualRepairing).toBe(false);
    expect(current.autoRecoveryPhase).toBe("verifying");
    expect(current.nextRetryAt).toBe(START.getTime() + 10_000);

    await advance(1_000);
    expect(current.autoRecoveryPhase).toBe("idle");
  });

  it("pauses after UAC cancellation and keeps status polling without another repair", async () => {
    vi.mocked(getSandboxStatus).mockResolvedValue(status(false));
    vi.mocked(repairSandboxBroker).mockRejectedValue(
      new ApiError(503, "安装已取消", "broker_uac_cancelled"),
    );
    render(<Harness />);
    await settle();

    await advance(10_000);
    expect(repairSandboxBroker).toHaveBeenCalledTimes(1);
    expect(current.autoRecoveryPhase).toBe("paused");
    expect(current.nextRetryAt).toBeNull();

    await advance(30_000);
    expect(getSandboxStatus).toHaveBeenCalledTimes(12);
    expect(repairSandboxBroker).toHaveBeenCalledTimes(1);
    expect(current.autoRecoveryPhase).toBe("paused");
  });

  it("deduplicates concurrent checks and clears recovery timers on unmount", async () => {
    let resolveStatus!: (value: SandboxBrokerStatus) => void;
    vi.mocked(getSandboxStatus).mockReturnValue(new Promise((resolve) => { resolveStatus = resolve; }));
    const view = render(<Harness />);

    const first = current.check();
    const second = current.check();
    expect(first).toBe(second);
    expect(getSandboxStatus).toHaveBeenCalledTimes(1);
    await act(async () => resolveStatus(status(false)));
    await settle();

    view.unmount();
    await advance(20_000);
    expect(repairSandboxBroker).not.toHaveBeenCalled();
  });
});
