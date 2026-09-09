import { Tooltip } from "antd";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ButtonTooltipContext } from "./ButtonTooltipContext";
import IconAction from "./IconAction";

describe("button tooltip scope", () => {
  it("keeps names and clicks while disabling only the scoped button hint", async () => {
    const click = vi.fn();
    render(<><ButtonTooltipContext.Provider value={false}><IconAction label="Side copy" icon={<span />} onClick={click} /><Tooltip title="Usage details"><span>Usage</span></Tooltip></ButtonTooltipContext.Provider><IconAction label="Main copy" icon={<span />} /></>);
    fireEvent.mouseEnter(screen.getByRole("button", { name: "Side copy" }));
    fireEvent.click(screen.getByRole("button", { name: "Side copy" }));
    expect(click).toHaveBeenCalledOnce();
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.mouseEnter(screen.getByText("Usage"));
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Usage details");
    fireEvent.mouseLeave(screen.getByText("Usage"));
    fireEvent.mouseEnter(screen.getByRole("button", { name: "Main copy" }));
    expect(await screen.findByText("Main copy")).toBeInTheDocument();
  });
});
