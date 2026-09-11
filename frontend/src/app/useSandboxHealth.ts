import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  getSandboxStatus,
  repairSandboxBroker,
  type SandboxBrokerStatus,
} from "../api";

export type SandboxHealthPhase = "checking" | "healthy" | "unhealthy";
export type SandboxAutoRecoveryPhase = "idle" | "observing" | "repairing" | "verifying" | "paused";

export interface SandboxHealthState {
  phase: SandboxHealthPhase;
  installed: boolean;
  code: string | null;
  detail: string | null;
  error_report?: import("../api/errorReport").ErrorReport | null;
  checking: boolean;
  autoRecoveryPhase: SandboxAutoRecoveryPhase;
  nextRetryAt: number | null;
  check: () => Promise<SandboxBrokerStatus>;
  notifyUserBackendRequest: () => void;
}

const POLL_DELAY_MS = 30_000;
const RECOVERY_CHECK_DELAY_MS = 1_000;
const RECOVERY_WINDOW_MS = 10_000;
const PAUSED_REPAIR_CODES = new Set(["broker_uac_cancelled", "broker_service_start_failed", "broker_service_state_failed"]);

function failedStatus(cause: unknown): SandboxBrokerStatus {
  return {
    installed: false,
    healthy: false,
    code: cause instanceof ApiError ? cause.code ?? "broker_status_failed" : "broker_status_failed",
    error_report: cause instanceof ApiError ? cause.error_report : undefined,
    detail: cause instanceof Error ? cause.message : "无法连接沙箱 Broker。",
  };
}

function statusIsHealthy(status: SandboxBrokerStatus): boolean {
  return status.installed && status.healthy;
}

export function useSandboxHealth(): SandboxHealthState {
  const [phase, setPhase] = useState<SandboxHealthPhase>("checking");
  const [installed, setInstalled] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  const [detail, setDetail] = useState<string | null>(null);
  const [report, setReport] = useState<import("../api/errorReport").ErrorReport | null>(null);
  const [checking, setChecking] = useState(true);
  const [autoRecoveryPhase, setAutoRecoveryPhase] = useState<SandboxAutoRecoveryPhase>("idle");
  const [nextRetryAt, setNextRetryAt] = useState<number | null>(null);
  const mountedRef = useRef(false);
  const phaseRef = useRef<SandboxHealthPhase>("checking");
  const autoRecoveryPhaseRef = useRef<SandboxAutoRecoveryPhase>("idle");
  const recoveryDeadlineRef = useRef<number | null>(null);
  const timerRef = useRef<ReturnType<typeof globalThis.setTimeout> | null>(null);
  const checkInFlightRef = useRef<Promise<SandboxBrokerStatus> | null>(null);
  const repairInFlightRef = useRef<Promise<void> | null>(null);
  const repairAfterCheckRef = useRef(false);
  const checkRef = useRef<() => Promise<SandboxBrokerStatus>>(() => Promise.resolve(failedStatus(null)));
  const repairRef = useRef<() => Promise<void>>(() => Promise.resolve());

  const cancelTimer = useCallback(() => {
    if (timerRef.current !== null) {
      globalThis.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const clearScheduled = useCallback(() => {
    cancelTimer();
    if (mountedRef.current) setNextRetryAt(null);
  }, [cancelTimer]);

  const updateAutoRecovery = useCallback((next: SandboxAutoRecoveryPhase, deadline: number | null) => {
    autoRecoveryPhaseRef.current = next;
    recoveryDeadlineRef.current = deadline;
    if (mountedRef.current) {
      setAutoRecoveryPhase(next);
      setNextRetryAt(deadline);
    }
  }, []);

  const scheduleCheck = useCallback((delay: number) => {
    if (!mountedRef.current) return;
    if (timerRef.current !== null) globalThis.clearTimeout(timerRef.current);
    timerRef.current = globalThis.setTimeout(() => {
      timerRef.current = null;
      void checkRef.current();
    }, delay);
  }, []);

  const schedulePoll = useCallback(() => {
    if (!mountedRef.current) return;
    if (!(["idle", "paused"] as SandboxAutoRecoveryPhase[]).includes(autoRecoveryPhaseRef.current)) return;
    clearScheduled();
    scheduleCheck(POLL_DELAY_MS);
  }, [clearScheduled, scheduleCheck]);

  const scheduleRecoveryCheck = useCallback(() => {
    const deadline = recoveryDeadlineRef.current;
    if (!mountedRef.current || deadline === null) return;
    const remaining = Math.max(0, deadline - Date.now());
    scheduleCheck(Math.min(RECOVERY_CHECK_DELAY_MS, remaining));
  }, [scheduleCheck]);

  const beginRecoveryWindow = useCallback((next: "observing" | "verifying") => {
    clearScheduled();
    const deadline = Date.now() + RECOVERY_WINDOW_MS;
    updateAutoRecovery(next, deadline);
    scheduleRecoveryCheck();
  }, [clearScheduled, scheduleRecoveryCheck, updateAutoRecovery]);

  const resetAutoRecovery = useCallback(() => {
    clearScheduled();
    updateAutoRecovery("idle", null);
  }, [clearScheduled, updateAutoRecovery]);

  const applyStatus = useCallback((status: SandboxBrokerStatus): boolean => {
    const healthy = statusIsHealthy(status);
    phaseRef.current = healthy ? "healthy" : "unhealthy";
    if (mountedRef.current && (healthy || autoRecoveryPhaseRef.current !== "paused")) {
      setInstalled(status.installed);
      setReport(healthy ? null : status.error_report ?? null);
      setCode(healthy ? null : status.code?.trim() || (
        status.installed ? "broker_unhealthy" : "broker_not_installed"
      ));
      setDetail(healthy ? null : status.detail || (
        status.installed ? "沙箱 Broker 健康检查未通过。" : "沙箱 Broker 未安装。"
      ));
      setPhase(healthy ? "healthy" : "unhealthy");
    }
    return healthy;
  }, []);

  const applyRepairFailure = useCallback((cause: unknown): string => {
    const failureCode = cause instanceof ApiError ? cause.code ?? "broker_install_failed" : "broker_install_failed";
    phaseRef.current = "unhealthy";
    if (mountedRef.current) {
      setCode(failureCode);
      setReport(cause instanceof ApiError ? cause.error_report ?? null : null);
      setDetail(cause instanceof Error ? cause.message : "沙箱 Broker 修复失败。");
      setPhase("unhealthy");
    }
    return failureCode;
  }, []);

  const handleCheckedStatus = useCallback((healthy: boolean) => {
    if (!mountedRef.current) return;
    if (healthy) {
      repairAfterCheckRef.current = false;
      resetAutoRecovery();
      schedulePoll();
      return;
    }
    if (autoRecoveryPhaseRef.current === "repairing") return;
    if (autoRecoveryPhaseRef.current === "paused") {
      schedulePoll();
      return;
    }
    if (autoRecoveryPhaseRef.current === "idle") {
      beginRecoveryWindow("observing");
      return;
    }
    const deadline = recoveryDeadlineRef.current;
    if (deadline !== null && Date.now() >= deadline) {
      clearScheduled();
      repairAfterCheckRef.current = true;
      return;
    }
    scheduleRecoveryCheck();
  }, [beginRecoveryWindow, clearScheduled, resetAutoRecovery, schedulePoll, scheduleRecoveryCheck]);

  const check = useCallback((): Promise<SandboxBrokerStatus> => {
    if (checkInFlightRef.current) return checkInFlightRef.current;
    cancelTimer();
    if (mountedRef.current) setChecking(true);
    const request = getSandboxStatus()
      .catch((cause) => failedStatus(cause))
      .then((status) => {
        handleCheckedStatus(applyStatus(status));
        return status;
      })
      .finally(() => {
        checkInFlightRef.current = null;
        if (mountedRef.current) setChecking(false);
        if (repairAfterCheckRef.current) {
          repairAfterCheckRef.current = false;
          void repairRef.current();
        }
      });
    checkInFlightRef.current = request;
    return request;
  }, [applyStatus, cancelTimer, handleCheckedStatus]);
  checkRef.current = check;

  const repair = useCallback((): Promise<void> => {
    if (repairInFlightRef.current) return repairInFlightRef.current;
    if (
      !mountedRef.current

      || phaseRef.current === "healthy"
      || autoRecoveryPhaseRef.current === "paused"
    ) {
      return Promise.resolve();
    }
    const request = (async () => {
      if (checkInFlightRef.current) await checkInFlightRef.current;
      if (
        !mountedRef.current

        || phaseRef.current === "healthy"
        || autoRecoveryPhaseRef.current === "paused"
      ) return;
      clearScheduled();
      updateAutoRecovery("repairing", null);
      try {
        await repairSandboxBroker();
      } catch (cause) {
        if (!mountedRef.current) return;
        const failureCode = applyRepairFailure(cause);
        if (PAUSED_REPAIR_CODES.has(failureCode)) {
          updateAutoRecovery("paused", null);
          schedulePoll();
          return;
        }
      }
      if (mountedRef.current) beginRecoveryWindow("verifying");
    })().finally(() => {
      repairInFlightRef.current = null;
    });
    repairInFlightRef.current = request;
    return request;
  }, [applyRepairFailure, beginRecoveryWindow, clearScheduled, schedulePoll, updateAutoRecovery]);
  repairRef.current = repair;

  const notifyUserBackendRequest = useCallback(() => {
    if (
      !mountedRef.current

      || autoRecoveryPhaseRef.current === "repairing"
    ) return;
    if (phaseRef.current !== "unhealthy" && autoRecoveryPhaseRef.current === "idle") return;
    cancelTimer();
    void checkRef.current();
  }, [cancelTimer]);

  useEffect(() => {
    mountedRef.current = true;
    void check();
    return () => {
      mountedRef.current = false;
      repairAfterCheckRef.current = false;
      if (timerRef.current !== null) {
        globalThis.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [check]);

  return {
    phase,
    installed,
    code,
    detail,
    error_report: report,
    checking,
    autoRecoveryPhase,
    nextRetryAt,
    check,
    notifyUserBackendRequest,
  };
}
