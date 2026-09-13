import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import DecisionCard from "./DecisionCard";

it("shows one-time escalation separately from ordinary tool approval", async () => {
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  const user = userEvent.setup();
  render(
    <DecisionCard
      request={{
        decision_id: "escalation-1",
        kind: "tool",
        approval_kind: "sandbox_escalation",
        tool: "run_command",
        arguments: { cmd: "Get-Content example.txt" },
        details: "Access is denied.",
      }}
      onSubmit={onSubmit}
    />,
  );
  expect(screen.getByText("工具提权")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "本会话允许" })).not.toBeInTheDocument();
  expect(screen.getByText(/之前已完成的操作可能重复执行/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "提权并重试一次" }));
  expect(onSubmit).toHaveBeenCalledWith("allow_once", {});
  await user.click(screen.getByRole("button", { name: "拒绝" }));
  expect(onSubmit).toHaveBeenLastCalledWith("deny", {});
});
