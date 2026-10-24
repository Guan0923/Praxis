import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import WorkbenchWelcome, { WORKBENCH_STARTERS } from "./WorkbenchWelcome";

describe("WorkbenchWelcome", () => {
  it("offers keyboard-accessible starters without automatically submitting", async () => {
    const user = userEvent.setup();
    const select = vi.fn();
    render(<WorkbenchWelcome onSelectPrompt={select} />);
    expect(screen.getByRole("region", { name: "Praxis 工作台" })).toBeInTheDocument();
    const explore = screen.getByRole("button", { name: /了解项目/ });
    explore.focus();
    await user.keyboard("{Enter}");
    expect(select).toHaveBeenCalledTimes(1);
    expect(select).toHaveBeenCalledWith(WORKBENCH_STARTERS[0].prompt);
    await user.click(screen.getByRole("button", { name: /实现想法/ }));
    expect(select).toHaveBeenLastCalledWith(WORKBENCH_STARTERS[1].prompt);
    await user.click(screen.getByRole("button", { name: /排查问题/ }));
    expect(select).toHaveBeenLastCalledWith(WORKBENCH_STARTERS[2].prompt);
    expect(screen.getByText("建议只会填入输入框，由你确认后发送。")).toBeInTheDocument();
  });

  it("supports a separate hero so the editor can sit before starter actions", () => {
    render(<WorkbenchWelcome heroOnly />);
    expect(screen.getByRole("region", { name: "Praxis 工作台" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /把想法交给 Praxis/ })).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("disables suggestions when the composer cannot accept input", async () => {
    const select = vi.fn();
    const { rerender } = render(<WorkbenchWelcome disabled onSelectPrompt={select} />);
    for (const button of screen.getAllByRole("button")) expect(button).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /了解项目/ }));
    expect(select).not.toHaveBeenCalled();
    rerender(<WorkbenchWelcome />);
    for (const button of screen.getAllByRole("button")) expect(button).toBeDisabled();
  });
});
